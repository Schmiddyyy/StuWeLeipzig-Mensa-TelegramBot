"""this Telegram bot has two purposes:
1. it crawls the Studentenwerk Leipzig for the daily meal plan
2. it can also fetch the exam scores from CampusDual using local credentials.

The daily meal can be scheduled to be sent every day at a specific time, per user.

The exam scores being sent is only meant for private use. it can only be called from (and sent to)
a single, hardcoded chat id, as the credentials are currently stored in clear text.
"""
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from warnings import filterwarnings

import scrapy
from pid import PidFile
from pid.base import PidFileAlreadyLockedError
from playwright._impl._api_types import Error as PlaywrightError
from playwright.async_api import async_playwright
from scrapyscript import Job, Processor
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.warnings import PTBUserWarning


def main():
    """sets up the bot (using token), defines bot command handlers, configures logging and warnings,
    and ensures only one instance is ever running.
    Finally, starts application event polling loop."""

    # silence warning that is already accounted for
    filterwarnings(
        action="ignore",
        category=PTBUserWarning,
        message=(
            "Prior to v20.0 the `days` parameter was not aligned to that of cron's weekday "
            "scheme.We recommend double checking if the passed value is correct."
        ),
    )

    # logging format config
    logging.basicConfig(
        level=logging.WARN,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # loading API token for Telegram bot access
    try:
        with open("token.txt", "r", encoding="utf8") as fobj:
            token = fobj.readline().strip()
    except FileNotFoundError:
        logging.critical(
            "'token.txt' missing. Create it and insert the token (without quotation marks)"
        )
        sys.exit()

    application = (
        ApplicationBuilder()
        .token(token)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # restoring all daily auto messages using chatids and times saved to jobs.db
    JobManager(application).load_jobs()

    start_handler = CommandHandler("start", start)
    application.add_handler(start_handler)

    heute_handler = CommandHandler("heute", heute)
    application.add_handler(heute_handler)

    morgen_handler = CommandHandler("morgen", morgen)
    application.add_handler(morgen_handler)

    uebermorgen_handler = CommandHandler("uebermorgen", uebermorgen)
    ubermorgen_handler = CommandHandler("ubermorgen", uebermorgen)
    application.add_handler(uebermorgen_handler)
    application.add_handler(ubermorgen_handler)

    subscribe_handler = CommandHandler("subscribe", subscribe)
    application.add_handler(subscribe_handler)

    unsubscribe_handler = CommandHandler("unsubscribe", unsubscribe)
    application.add_handler(unsubscribe_handler)

    changetime_handler = CommandHandler("changetime", changetime)
    application.add_handler(changetime_handler)

    send_mealjob_time_handler = CommandHandler("when", send_mealjob_time)
    application.add_handler(send_mealjob_time_handler)

    force_get_new_grades_handler = CommandHandler("cd", force_get_new_grades)
    application.add_handler(force_get_new_grades_handler)

    ack_handler = CommandHandler("ack", acknowledge)
    application.add_handler(ack_handler)

    application.job_queue.run_repeating(
        callback=job_send_new_grades, interval=300, chat_id=578278860
    )

    application.run_polling()


class JobManager:
    """manages restoring jobs at startup, creating new jobs and unloading jobs.
    these jobs refer to automatically sending today's meal to a subscribed chat id,
    using a user-defined time of day"""

    con = sqlite3.connect("jobs.db")
    cur = con.cursor()
    loaded_jobs = {}
    application = None

    def __init__(self, application=None):
        """application ref is optional and is used to load/unload jobs
        application is passed at"""
        if application is not None:
            JobManager.application = application

    def conv_to_utc(self, hour, minute) -> time:
        """PTB uses utc time internally, so MESZ has to be converted first"""

        # using current day as basis for conversion
        # this WILL fail any time the timezone changes after the job has been loaded
        # a bot restart is sufficient as a workaround
        local_date = datetime.now().replace(hour=hour, minute=minute)

        # converting date to UTC
        utc_date = local_date.astimezone(tz=timezone.utc)

        # extracting time from date
        utc_time = time(hour=utc_date.hour, minute=utc_date.minute)

        return utc_time

    def load_jobs(self) -> None:
        """restores jobs from from DB using chat_id, hour, min
        also converts hour/min from De/Berlin to utc time"""
        try:
            data = JobManager.cur.execute(
                "select id, hour, min from chatids"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logging.critical("sqlite3 is not properly set up: '%s'", str(exc))
            logging.critical("run DB_RESET.py to reset (will delete everything)")
            sys.exit()

        for line in data:
            utc_time = self.conv_to_utc(hour=int(line[1]), minute=int(line[2]))

            job_reference = JobManager.application.job_queue.run_daily(
                callback=job_send_today_meals,
                time=utc_time,
                days=(1, 2, 3, 4, 5),
                chat_id=int(line[0]),
            )
            JobManager.loaded_jobs[int(line[0])] = job_reference

    def add_job(self, chat_id: int, hour: int, minute: int) -> None:
        """adds a job to DB and then loads it"""

        JobManager.cur.execute(
            "insert into chatids values(?,?,?)", [chat_id, hour, minute]
        )
        JobManager.con.commit()

        utc_time = self.conv_to_utc(hour=hour, minute=minute)

        # starting the job
        job_reference = JobManager.application.job_queue.run_daily(
            callback=job_send_today_meals,
            time=utc_time,
            days=(1, 2, 3, 4, 5),
            chat_id=chat_id,
        )

        # saving the ref to loaded job (so that it can be unloaded)
        JobManager.loaded_jobs[chat_id] = job_reference

    def remove_job(self, chat_id: int) -> None:
        """removes a job from DB and then unloads it"""

        JobManager.cur.execute("delete from chatids where id = (?)", [chat_id])
        JobManager.con.commit()

        JobManager.loaded_jobs[chat_id].schedule_removal()
        del JobManager.loaded_jobs[chat_id]

    def get_job_times(self) -> tuple[str, str]:
        """retrieves the time at which a job for a given chat id will be run"""

        ##### retrieval from DB. This assumes that time is parsed && job is loaded correctly
        # job_time = JobManager.cur.execute(
        #     "select hour,minute from chatids where id=(?)", [chat_id]
        # ).fetchone()
        # formatted_string = str(hour) + ":" + str(minute) + " Uhr"

        # ugly
        formatted_string = f"count: {len(JobManager.loaded_jobs)}\n"

        for job_item in JobManager.loaded_jobs.items():
            formatted_string += str(job_item[1].job.trigger) + "\n"

        return formatted_string


###### crawler setup and stuff
class MensaSpider(scrapy.Spider):
    """scrapy Spider instance that scrapes data from Studentenwerk Leipzig.
    Has to be instantiated using URL with date parameter"""

    name = "mensaplan"

    def parse(self, response, **kwargs):
        result = {
            "date": "",
            "meals": []
        }

        # extracted date from website, to verify if it matches requested date
        result["date"] = response.css(
            'select#edit-date>option[selected="selected"]::text'
        ).get()

        for header in response.css("h3.title-prim"):
            meal = {
                "type": "",
                "name": "",
                "additional_ingredients": [],
                "prices": ""
            }

            # type of meal, like 'vegetarian'
            meal["type"] = header.xpath("text()").get()

            for subitem in header.xpath("following-sibling::*"):
                # title-prim ≙ begin of next menu type/end of this menu → stop processing
                if subitem.attrib == {"class": "title-prim"}:
                    break
                # accordion u-block: top-level item of a meal type
                # (usually there just is 1 u-block but there can be multiple)
                if subitem.attrib == {"class": "accordion u-block"}:
                    for subsubitem in subitem.xpath("child::section"):

                        meal["name"] = subsubitem.xpath("header/div/div/h4/text()").get()
                        meal["additional_ingredients"] = subsubitem.xpath(
                            "details/ul/li/text()"
                        ).getall()
                        meal["prices"] = (
                            subsubitem.xpath("header/div/div/p/text()[2]").get().strip()
                        )
                        # saving extracted data to individual meal data
                        result["meals"].append(meal)

        yield result


def mensa_data_to_string(mensa_data, using_date) -> str:
    """formats the raw data that is returned from MensaSpider.
    Also check the date of returned data, since the site falls back to
    current date instead of requested date if it is too far in the future"""

    sub_message = ""

    # when a date is requested that is too far in the future, the site will load the current date
    # therefore, if the date reported by the site (inside data{})
    # is != using_date, no plan for that date is available.
    if len(mensa_data[0]) == 1 or mensa_data[0]["date"].split(",")[
        1
    ].strip() != using_date.strftime("%d.%m.%Y"):
        sub_message += "Für diesen Tag existiert noch kein Plan.\n"

    else:
        # generating sub_message from spider results
        for meal in mensa_data[0]['meals']:
            # # # # # # if result == "date":
            # # # # # #     continue

            # meal["type"]: vegetarian/meat/free choice
            sub_message += "\n*" + meal["type"] + ":*\n"
            
            # meal name (will break for multi-item dishes)
            sub_message += " •__ " + meal["name"] + "__\n"

            # add. ingredients
            for ingredient in meal["additional_ingredients"]:
                sub_message += "     + _" + ingredient + "_\n"
            
            # 
            sub_message += "   " + meal["prices"] + "\n"

            
            # # # # # # # # # # usually a type only has one meal, except for the 'free choice' type of meals
            # # # # # # # # # for main_meal in mensa_data[0][result]:
            # # # # # # # # #     # name of meal (or. name of subitem for free choice meal)
            # # # # # # # # #     sub_message += " •__ " + main_meal[0] + "__\n"
            # # # # # # # # #     # components or ingredients of a meal (accessible using '+' on page)
            # # # # # # # # #     for additional_ingredient in main_meal[1]:
            # # # # # # # # #         sub_message += "     + _" + additional_ingredient + "_\n"
            # # # # # # # # #     # price of meal or subitem
            # # # # # # # # #     sub_message += "   " + main_meal[2] + "\n"

    return sub_message


def generate_mensa_message(input_date: date, user_aware_future_day: bool = False):
    """First, day of week in input_date is evaluated: if Saturday/Sunday,
    override date to next monday and add a notice to the message that the day was overridden.
    That message is only added if user_aware_future_day is not True

    Then, mensa crawler is called for selected date. If returned data is actually for that date,
    that data will be parsed and appended to message"""

    message = ""
    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]

    # the Mensa ID to crawl
    mensen_ids = {
        # "Alle Mensen": "all", ### currently unsupported
        "Dittrichring": 153,
        "Botanischen Garten": 127,
        "Academica": 118,
        "am Park": 106,
        "Elsterbecken": 115,
        "Medizincampus": 162,
        "Peterssteinweg": 111,
        "Schoenauer Str": 140,
        "Tierklinik": 170,
    }
    location = mensen_ids["am Park"]

    # Heute ist Mo-Fr (fooden)
    if input_date.isoweekday() <= 5:
        using_date = input_date
        message += (
            "_"
            + weekdays[using_date.isoweekday() - 1]
            + using_date.strftime(", %d.%m.%Y")
            + "_\n"
        )

    # Samstag → Plan für übermorgen laden
    elif input_date.isoweekday() == 6:
        using_date = input_date + timedelta(days=2)
        message += (
            "_"
            + weekdays[using_date.isoweekday() - 1]
            + using_date.strftime(", %d.%m.%Y")
        )
        if user_aware_future_day:
            message += "_\n"
        else:
            message += " (Übermorgen)_\n"

    # Sonntag → Plan für morgen laden
    elif input_date.isoweekday() == 7:
        using_date = input_date + timedelta(days=1)
        message += (
            "_"
            + weekdays[using_date.isoweekday() - 1]
            + using_date.strftime(", %d.%m.%Y")
        )

        if user_aware_future_day:
            message += "_\n"
        else:
            message += " (Morgen)_\n"

    job = Job(
        MensaSpider,
        start_urls=[
            (
                "https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan"
                f"?location={str(location)}&date={str(using_date)}"
            )
        ],
    )
    processor = Processor(settings=None)
    mensa_data = processor.run(job)
    formatted_mensa_data = mensa_data_to_string(
        mensa_data=mensa_data, using_date=using_date
    )

    message += formatted_mensa_data

    message += "\n < /heute >  < /morgen >"
    message += "\n < /uebermorgen >"

    message = markdown_v2_formatter(message)
    return message


def markdown_v2_formatter(text: str) -> str:
    """used for escaping special Markdown V2 characters using backslash.
    can only be used for strings that should not contain formatting,
    as formatting is defined using these characters"""

    text = text.replace(".", r"\.")
    text = text.replace("!", r"\!")
    text = text.replace("+", r"\+")
    text = text.replace("-", r"\-")
    text = text.replace("<", r"\<")
    text = text.replace(">", r"\>")
    text = text.replace("(", r"\(")
    text = text.replace(")", r"\)")
    text = text.replace("=", r"\=")

    return text


def parse_time(str_time: str) -> tuple[int, int]:
    """checks time string for validity (regex), and if successful, returns ints Hours, Mins"""

    pattern = "([01]?[0-9]|2[0-3]):[0-5][0-9]"

    match = re.match(pattern, str_time)

    if not match:
        raise ValueError

    return [int(x) for x in match.group().split(":")]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram command that gets send automatically when first 'contacting' the bot.
    sends information on how to use it."""

    start_text = """
/subscribe: automatische Nachrichten aktivieren.
/unsubscribe: automatische Nachrichten deaktivieren.
/heute: manuell aktuelles Angebot anzeigen.
/morgen: morgiges Angebot anzeigen.

Wenn /heute oder /morgen kein Wochentag ist, wird der Plan für Montag angezeigt.
    """
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=start_text, parse_mode=ParseMode.MARKDOWN
    )

    await subscribe(update=update, context=context)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram command to enable automatic delivery of meal info (every day except sat/sun).
    Called by default when using the first time, and defaults to 06:00 AM
    Command: '/subscribe [Opt: HH:MM]'"""

    chat_id = update.effective_chat.id

    if len(context.args) != 0:
        try:
            hour, minute = parse_time(context.args[0])
        except ValueError:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Eingegebene Zeit ist ungültig.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        # "6:00 Uhr" as default
        hour, minute = (6, 0)

    try:
        job_manager = JobManager()
        job_manager.add_job(chat_id=chat_id, hour=hour, minute=minute)
        message = (
            f"Plan wird ab jetzt automatisch an Wochentagen {hour:02}:{minute:02} Uhr gesendet."
            "\n\n/changetime [[Zeit]] zum Ändern\n/unsubscribe zum Deaktivieren"
        )

    # key (chatid) already exists
    except sqlite3.IntegrityError:
        message = (
            "Automatische Nachrichten sind schon aktiviert."
            "\nZum Ändern der Zeit: /changetime [[Zeit]]"
        )

    # confirmation message
    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram command to disable automatic delivery of meal info'"""

    chat_id = update.effective_chat.id

    try:
        job_manager = JobManager()
        job_manager.remove_job(chat_id)
        # confirmation message
        await context.bot.send_message(
            chat_id=chat_id,
            text="Plan wird nicht mehr automatisch gesendet.",
            parse_mode=ParseMode.MARKDOWN,
        )

    except KeyError:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Automatische Nachrichten waren bereits deaktiviert.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def changetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram command to change the time at which todays meals will be sent automatically.
    Command: '/changetime'"""

    chat_id = update.effective_chat.id
    job_manager = JobManager()

    current_time = job_manager.get_job_times()

    if current_time is None:
        message = (
            "Automatische Nachrichten sind noch nicht aktiviert."
            "\n/subscribe oder\n/subscribe [[Zeit]] ausführen"
        )

    elif len(context.args) != 0:
        try:
            hour, minute = parse_time(context.args[0])
            job_manager = JobManager()

            # unload job and remove from DB
            job_manager.remove_job(chat_id=chat_id)
            # creating and loading new job, and adding to DB
            job_manager.add_job(chat_id=chat_id, hour=hour, minute=minute)

            message = (
                "Plan wird ab jetzt automatisch an Wochentagen "
                f"{hour:02}:{minute:02} Uhr gesendet."
            )

        except ValueError:
            message = "Eingegebene Zeit ist ungültig."

    else:
        message = "Bitte Zeit eingegeben\n/changetime [[Zeit]]"

    # confirmation message
    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def heute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram command to manually get today's available meals
    Command: '/heute'"""

    message = generate_mensa_message(date.today())

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def morgen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram command to manually get tomorrows available meals
    Command: '/morgen'"""

    message = generate_mensa_message(
        date.today() + timedelta(days=1), user_aware_future_day=True
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def uebermorgen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram command to manually get meals 2 days in the future
    Commands: '/uebermorgen' '/ubermorgen'"""

    message = generate_mensa_message(
        date.today() + timedelta(days=2), user_aware_future_day=True
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def send_mealjob_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(debug) Telegram command that sends the time at which todays meal will be sent.
    Command: '/when'"""

    chat_id = update.effective_chat.id
    job_manager = JobManager()

    job_time = job_manager.get_job_times()
    if job_time is not None:
        message = job_time
    else:
        message = "Plan wird nicht automatisch gesendet"

    # print(JobManager.loaded_jobs[0])

    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


# used as callback when called automatically (daily)
async def job_send_today_meals(context: ContextTypes.DEFAULT_TYPE) -> None:
    """callback job that fetches, formats and sends todays meals
    to appropriate user at chosen time of day"""

    message = generate_mensa_message(date.today())

    await context.bot.send_message(
        chat_id=context.job.chat_id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def playwright_fetch_grades() -> list:
    """private use function: retrieves exam results from CampusDual using local creds."""

    with open("login_creds.txt", "r", encoding="utf8") as fobj:
        uname, password = fobj.readline().strip().split(",")

    grades = []

    async with async_playwright() as playwright:
        # setting up browser
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        # login page
        await page.goto(
            (
                "https://erp.campus-dual.de/sap/bc/webdynpro/sap/zba_initss?sap-client=100"
                "&sap-language=de&uri=https://selfservice.campus-dual.de/index/login"
            )
        )
        await page.get_by_role("textbox", name="Benutzer").click()
        await page.get_by_role("textbox", name="Benutzer").fill(uname)
        await page.locator("#sap-password").click()
        await page.get_by_role("textbox", name="Kennwort").fill(password)
        await page.get_by_role("button", name="Anmelden").click()

        # exam results page
        await page.goto("https://selfservice.campus-dual.de/acwork/index")

        table = page.locator("#acwork tbody")
        top_level_lines = table.locator(".child-of-node-0")

        for i in range(await top_level_lines.count()):
            top_level_line_id = await top_level_lines.nth(i).get_attribute("id")
            top_level_line_contents = top_level_lines.nth(i).locator("td")

            name = await top_level_line_contents.nth(0).inner_text()
            grade = await top_level_line_contents.nth(1).inner_text()
            count_sublines = await table.locator(
                f".child-of-{top_level_line_id}"
            ).count()

            # returning name of course, received (aggregate) grade, and amount of sub grades
            # (as a newly released sub grade doesn't always change aggregate score)
            grades.append((name, grade, str(count_sublines)))

        await browser.close()

        return grades


async def job_send_new_grades(context: ContextTypes.DEFAULT_TYPE):
    """private use job that triggers retrieval of grades from CampusDual, then formats message"""

    if not context.job.chat_id == 578278860:
        return

    message = ""
    try:
        grades = await playwright_fetch_grades()

        acknowlegded = []
        with open("acknowledged.txt", "r", encoding="utf8") as fobj:
            for line in fobj:
                acknowlegded.append(
                    (line.split(";")[0], line.split(";")[1], line.split(";")[2].strip())
                )

        for grade in grades:
            if grade not in acknowlegded:
                message += f"\n{grade[1]}:\n{grade[0]}\n"

        if message:
            message = "Neue Ergebnisse:\n" + message
            await context.bot.send_message(
                chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
            )

    except PlaywrightError as exc:
        if exc.message.startswith("net::ERR_ADDRESS_UNREACHABLE"):
            logging.warning("CampusDual is likely offline:\n%s", exc.message)
        else:
            logging.warning("couldn't interact with CampusDual:\n%s", exc.message)


async def force_get_new_grades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """private use function that retrieves grades from CampusDual, then formats message.
    ignores already approved grades, and is meant as a debugging function"""

    if not update.effective_chat.id == 578278860:
        return

    message = ""
    try:
        grades = await playwright_fetch_grades()
        for grade in grades:
            message += f"\n{grade[0]}\n{grade[1]}\n{grade[2]}\n"

    except PlaywrightError as exc:
        if exc.message.startswith("net::ERR_ADDRESS_UNREACHABLE"):
            message = f"CampusDual is likely offline:\n{exc.message}"
        else:
            message = f"couldn't interact with CampusDual:\n{exc.message}"

    await context.bot.send_message(
        chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def acknowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(private) Telegram command to acknowledge all current grades, so they won't be sent again
    Command: '/ack [command]'"""

    if not update.effective_chat.id == 578278860:
        return

    message = ""

    if not context.args:
        # get currently acknowledged
        with open("acknowledged.txt", "r", encoding="utf8") as fobj:
            linecount = 0

            for line in fobj:
                message += f'{line.split(";")[1]}: {line.split(";")[0]}\n'
                linecount += 1

            if linecount == 0:
                message += "keine Acknowledgements vorhanden"

    elif context.args[0] == "reset":
        with open("acknowledged.txt", "w", encoding="utf8") as fobj:
            message += "Acknowledgements have been reset"

    elif context.args[0] == "all":
        grades = await playwright_fetch_grades()
        with open("acknowledged.txt", "w", encoding="utf8") as fobj:
            for grade in grades:
                fobj.write(f"{grade[0]};{grade[1]};{grade[2]}\n")
        message += "Alle aktuellen Ergebnisse werden ignoriert"

    await context.bot.send_message(
        chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
    )


if __name__ == "__main__":
    try:
        # prevents multiple instances of this script to run at the same time
        # → easy way to restart in case of error
        with PidFile():
            main()

    # if pidfile exists ≙ program is already running: catch the pidfilelocked exc, cleanly exit
    except PidFileAlreadyLockedError:
        sys.exit()
