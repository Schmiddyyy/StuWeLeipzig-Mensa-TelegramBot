import logging
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from warnings import filterwarnings

import scrapy
from pid import PidFile
from pid.base import PidFileAlreadyLockedError
from playwright.async_api import async_playwright
from scrapyscript import Job, Processor
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.warnings import PTBUserWarning


class JobManager:
    con = sqlite3.connect("jobs.db")
    cur = con.cursor()
    loaded_jobs = dict()

    def load_jobs(self) -> None:
        try:
            data = JobManager.cur.execute("select * from chatids").fetchall()
        except sqlite3.OperationalError as e:
            logging.critical(f"sqlite3 is not properly set up. Exception: '{str(e)}'")
            logging.critical("run DB_RESET.py to reset (will delete everything)")
            exit()

        for line in data:
            try:
                localDate = datetime.now().replace(
                    hour=int(line[1]), minute=int(line[2])
                )
                utcDate = localDate.astimezone(tz=timezone.utc)
                utcTime = time(hour=utcDate.hour, minute=utcDate.minute)

                jobReference = application.job_queue.run_daily(
                    callback=callback_heute, time=utcTime, chat_id=int(line[0])
                )
                JobManager.loaded_jobs[int(line[0])] = jobReference

            except Exception as e:
                logging.critical(
                    f"Failed to load jobs from sqlite3. Exception: {str(e)}"
                )
                logging.critical("Manually inspect table 'chatids' for malformed data")
                exit()

    def add_job(self, id: int, hour: int, min: int) -> None:
        JobManager.cur.execute("insert into chatids values(?,?,?)", [id, hour, min])
        JobManager.con.commit()

        # starting the job
        localDate = datetime.now().replace(hour=int(hour), minute=int(min))
        utcDate = localDate.astimezone(tz=timezone.utc)
        utcTime = time(hour=utcDate.hour, minute=utcDate.minute)

        jobReference = application.job_queue.run_daily(
            callback=callback_heute, time=utcTime, days=(1, 2, 3, 4, 5), chat_id=id
        )

        # saving the ref to loaded job (so that it can be unloaded)
        JobManager.loaded_jobs[id] = jobReference

        def remove_job(self):
            pass

    def remove_job(self, id: int) -> None:
        JobManager.cur.execute("delete from chatids where id = (?)", [id])
        JobManager.con.commit()

        JobManager.loaded_jobs[id].schedule_removal()
        del JobManager.loaded_jobs[id]

    def get_job_time(self, chat_id: int) -> tuple[str, str]:
        time = JobManager.cur.execute(
            "select hour,min from chatids where id=(?)", [chat_id]
        ).fetchone()
        return time


def main():
    # silence warning that i'm aware of
    filterwarnings(
        action="ignore",
        category=PTBUserWarning,
        message="Prior to v20.0 the `days` parameter was not aligned to that of cron's weekday scheme.We recommend double checking if the passed value is correct.",
    )

    # if pidfile exists ≙ program is already running: catch the pidfilelocked exc, exit()
    try:
        # prevents multiple instances of this script to run at the same time → easy way to restart in case of error
        with PidFile():

            # logging format config
            logging.basicConfig(
                level=logging.WARN,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )

            # loading API token for Telegram bot access
            try:
                with open("token.txt", "r") as fobj:
                    token = fobj.readline().strip()
            except FileNotFoundError:
                logging.critical(
                    "'token.txt' missing. Create it and insert the token (without quotation marks)"
                )
                exit()

            # :(
            global application
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
            job_manager = JobManager()
            job_manager.load_jobs()

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

            send_job_time_handler = CommandHandler("when", send_job_time)
            application.add_handler(send_job_time_handler)

            force_get_new_grades_handler = CommandHandler("cd", force_get_new_grades)
            application.add_handler(force_get_new_grades_handler)

            ack_handler = CommandHandler("ack", acknowledge)
            application.add_handler(ack_handler)

            application.job_queue.run_repeating(
                callback=job_get_new_grades, interval=300, chat_id=578278860
            )

            application.run_polling()

    except PidFileAlreadyLockedError:
        exit()


# escaping special Markdown V2 chars
def Markdown2Formatter(text: str) -> str:
    text = text.replace(".", "\.")
    text = text.replace("!", "\!")
    text = text.replace("+", "\+")
    text = text.replace("-", "\-")
    text = text.replace("<", "\<")
    text = text.replace(">", "\>")
    text = text.replace("(", "\(")
    text = text.replace(")", "\)")

    return text


def createMessageStringFromSpider(date: datetime.date, morgen: bool = False):

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
    location = mensen_ids["Schoenauer Str"]

    # Heute ist Mo-Fr (fooden)
    if date.isoweekday() <= 5:
        dataDate = date
        message += (
            "_"
            + weekdays[dataDate.isoweekday() - 1]
            + dataDate.strftime(", %d.%m.%Y")
            + "_\n"
        )

    # Samstag → Plan für übermorgen laden
    elif date.isoweekday() == 6:
        dataDate = date + timedelta(days=2)
        message += (
            "_" + weekdays[dataDate.isoweekday() - 1] + dataDate.strftime(", %d.%m.%Y")
        )
        if morgen:
            message += "_\n"
        else:
            message += " (Übermorgen)_\n"

    # Sonntag → Plan für morgen laden
    elif date.isoweekday() == 7:
        dataDate = date + timedelta(days=1)
        message += (
            "_" + weekdays[dataDate.isoweekday() - 1] + dataDate.strftime(", %d.%m.%Y")
        )

        if morgen:
            message += "_\n"
        else:
            message += " (Morgen)_\n"

    job = Job(
        MensaSpider,
        start_urls=[
            f"https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan?location={str(location)}&date={str(dataDate)}"
        ],
    )
    processor = Processor(settings=None)
    data = processor.run(job)

    # when a date is requested that is too far in the future, the site will load the current date
    # therefore, if the date reported by the site (inside data{}) is != dataDate, no plan for that date is available.
    if len(data[0]) == 1 or data[0]["date"].split(",")[1].strip() != dataDate.strftime(
        "%d.%m.%Y"
    ):
        message += "Für diesen Tag existiert noch kein Plan."

    else:
        # generating message from spider results
        for result in data[0]:
            if result == "date":
                continue

            # Art, zb. "Vegetarisches Gericht"
            message += "\n*" + result + ":*\n"
            # the actual meal - usually a type only has one meal, except for the 'free choice' type of meals
            for actualMeal in data[0][result]:
                # Name des Gerichts (bzw. des 'Teilgerichts' bei Gericht mit freier Auswahl)
                message += " •__ " + actualMeal[0] + "__\n"
                # Bestandteile/Zutaten des Gerichts (Sichtbar wenn '+' auf Seite geklickt)
                for additionalIngredient in actualMeal[1]:
                    message += "     + _" + additionalIngredient + "_\n"
                # Preis des Gerichts
                message += "   " + actualMeal[2] + "\n"

        message += "\n < /heute >  < /morgen >"
        message += "\n < /uebermorgen >"

    message = Markdown2Formatter(message)
    return message


###### crawler setup and stuff
class MensaSpider(scrapy.Spider):
    name = "mensaplan"

    def parse(self, response):
        result = {}

        result["date"] = response.css(
            'select#edit-date>option[selected="selected"]::text'
        ).get()

        for header in response.css("h3.title-prim"):
            name = header.xpath("text()").get()

            result[name] = []

            for subitem in header.xpath("following-sibling::*"):
                # title-prim ≙ begin of next menu type/end of this menu → stop processing
                if subitem.attrib == {"class": "title-prim"}:
                    break
                # accordion u-block: top-level item of a meal type (usually there just is 1 u-block but there can be multiple)
                elif subitem.attrib == {"class": "accordion u-block"}:
                    for subsubitem in subitem.xpath("child::section"):

                        title = subsubitem.xpath("header/div/div/h4/text()").get()
                        additionalIngredients = subsubitem.xpath(
                            "details/ul/li/text()"
                        ).getall()
                        price = (
                            subsubitem.xpath("header/div/div/p/text()[2]").get().strip()
                        )

                        result[name].append((title, additionalIngredients, price))

        yield result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

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


async def heute(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = createMessageStringFromSpider(date.today())

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def morgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(
        date.today() + timedelta(days=1), morgen=True
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def uebermorgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(
        date.today() + timedelta(days=2), morgen=True
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def changetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_manager = JobManager()

    current_time = job_manager.get_job_time(chat_id)

    if current_time is None:
        message = "Automatische Nachrichten sind noch nicht aktiviert.\n/subscribe oder\n/subscribe \[Zeit] ausführen"

    elif len(context.args) != 0:
        try:
            hour, min = parseTime(context.args[0])
            job_manager = JobManager()

            # unload job and remove from DB
            job_manager.remove_job(id=chat_id)
            # creating and loading new job, and adding to DB
            job_manager.add_job(id=chat_id, hour=hour, min=min)

            message = (
                "Plan wird ab jetzt automatisch an Wochentagen "
                + hour
                + ":"
                + min
                + " Uhr gesendet."
            )

        # except KeyError:
        #     await context.bot.send_message(chat_id=chat_id, text="Automatische Nachrichten sind noch nicht aktiviert.\n/subscribe oder\n/subscribe \[Zeit] ausführen", parse_mode=ParseMode.MARKDOWN)
        #     return
        except ValueError:
            message = "Eingegebene Zeit ist ungültig."
            # message = "Automatische Nachrichten sind noch nicht aktiviert.\n/subscribe oder\n/subscribe \[Zeit] ausführen"

    else:
        message = "Bitte Zeit eingegeben\n( /changetime \[Zeit] )"

    # confirmation message
    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def send_job_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    job_manager = JobManager()

    time = job_manager.get_job_time(chat_id)
    if time is not None:
        message = str(time[0]) + ":" + str(time[1]) + " Uhr"
    else:
        message = "Plan wird nicht automatisch gesendet"

    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


# checks time string for validity, and if successful, returns ints Hours, Mins
def parseTime(strTime: str) -> tuple[int, int]:
    regex = "([01]?[0-9]|2[0-3]):[0-5][0-9]"
    cregex = re.compile(regex)

    m = re.match(cregex, strTime)

    if m is not None:
        return m.group().split(":")
    else:
        raise ValueError


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id

    if len(context.args) != 0:
        try:
            hour, min = parseTime(context.args[0])
        except ValueError:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Eingegebene Zeit ist ungültig.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        hour, min = ("6", "00")

    try:
        job_manager = JobManager()
        job_manager.add_job(chat_id, hour, min)
        message = (
            "Plan wird ab jetzt automatisch an Wochentagen "
            + hour
            + ":"
            + min
            + " Uhr gesendet.\n\n/changetime \[Zeit] zum Ändern\n/unsubscribe zum Deaktivieren"
        )

    except sqlite3.IntegrityError:
        message = "Automatische Nachrichten sind schon aktiviert.\n(Zum Ändern der Zeit: /changetime \[Zeit])"

    # confirmation message
    await context.bot.send_message(
        chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


# used as callback when called automatically (daily)
async def callback_heute(context) -> None:
    job = context.job
    message = createMessageStringFromSpider(date.today())

    await context.bot.send_message(
        job.chat_id, text=message, parse_mode=ParseMode.MARKDOWN_V2
    )


async def playwright_fetch_grades() -> list:
    with open("login_creds.txt", "r") as fobj:
        uname, pw = fobj.readline().strip().split(",")

    grades = list()

    async with async_playwright() as p:
        # setting up browser
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # login page
        await page.goto(
            "https://erp.campus-dual.de/sap/bc/webdynpro/sap/zba_initss?sap-client=100&sap-language=de&uri=https://selfservice.campus-dual.de/index/login"
        )
        await page.get_by_role("textbox", name="Benutzer").click()
        await page.get_by_role("textbox", name="Benutzer").fill(uname)
        await page.locator("#sap-password").click()
        await page.get_by_role("textbox", name="Kennwort").fill(pw)
        await page.get_by_role("button", name="Anmelden").click()

        # exam results page
        await page.goto("https://selfservice.campus-dual.de/acwork/index")

        table = page.locator("#acwork tbody")
        top_level_lines = table.locator(".child-of-node-0")

        count_top_level_lines = await top_level_lines.count()

        for i in range(count_top_level_lines):
            top_level_line = top_level_lines.nth(i)
            top_level_line_id = await top_level_line.get_attribute("id")
            top_level_line_contents = top_level_line.locator("td")

            name = await top_level_line_contents.nth(0).inner_text()
            grade = await top_level_line_contents.nth(1).inner_text()
            count_sublines = await table.locator(
                f".child-of-{top_level_line_id}"
            ).count()

            # returning name of course, received (aggregate) grade, and amount of sub grades (as a newly released sub grade doesn't always change aggregate score)
            grades.append((name, grade, str(count_sublines)))

        await context.close()
        await browser.close()

        return grades


async def job_get_new_grades(context: ContextTypes.DEFAULT_TYPE):
    if not context._chat_id == 578278860:
        return

    message = ""
    grades = await playwright_fetch_grades()

    acknowlegded = list()
    with open("acknowledged.txt", "r") as fobj:
        for line in fobj:
            acknowlegded.append(
                (line.split(";")[0], line.split(";")[1], line.split(";")[2].strip())
            )

    for grade in grades:
        if grade not in acknowlegded:
            message += f"\n{grade[1]}:\n{grade[0]}\n"

    if message:
        message = "Neue Ergebnisse:\n\n" + message
        await context.bot.send_message(
            chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
        )


async def force_get_new_grades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context._chat_id == 578278860:
        return

    message = ""
    grades = await playwright_fetch_grades()

    for grade in grades:
        message += f"\n{grade[0]}\n{grade[1]}\n{grade[2]}\n"

    await context.bot.send_message(
        chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
    )


async def acknowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context._chat_id == 578278860:
        return

    message = ""

    if not context.args:
        # get currently acknowledged
        with open("acknowledged.txt", "r") as fobj:
            linecount = int()

            for line in fobj:
                message += f'{line.split(";")[1]}: {line.split(";")[0]}\n'
                linecount += 1

            if linecount == 0:
                message += "keine Acknowledgements vorhanden"

    elif context.args[0] == "reset":
        with open("acknowledged.txt", "w") as fobj:
            message += "Acknowledgements have been reset"

    elif context.args[0] == "all":
        grades = await playwright_fetch_grades()
        with open("acknowledged.txt", "w") as fobj:
            for grade in grades:
                fobj.write(f"{grade[0]};{grade[1]};{grade[2]}\n")
        message += "Alle aktuellen Ergebnisse werden ignoriert"

    await context.bot.send_message(
        chat_id=578278860, text=message, parse_mode=ParseMode.MARKDOWN
    )


if __name__ == "__main__":
    main()
