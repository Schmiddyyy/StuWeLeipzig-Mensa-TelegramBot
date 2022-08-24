from pid import PidFile
from pid.base import PidFileAlreadyLockedError


import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

import scrapy
from scrapyscript import Job, Processor


from datetime import datetime, time, date, timedelta, timezone

import re




# job DB init
import sqlite3
con = sqlite3.connect("jobs.db")
cur = con.cursor()


logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')




loadedJobs = {}





def registerJob(id, hour, min):

    cur.execute("insert into chatids values(?,?,?)", [id, hour, min])
    con.commit()


    # starting the job
    localDate = datetime.now().replace(hour=int(hour), minute=int(min))
    utcDate = localDate.astimezone(tz=timezone.utc)
    utcTime = time(hour=utcDate.hour, minute=utcDate.minute)

    jobReference = application.job_queue.run_daily(callback=callback_heute, time=utcTime, days=(1,2,3,4,5), chat_id=id)

    # saving the ref to loaded job (so that it can be unloaded)
    loadedJobs[id] = jobReference

    



def unregisterJob(id):

    cur.execute("delete from chatids where id = (?)", [id])
    con.commit()

    loadedJobs[id].schedule_removal()
    loadedJobs.pop(id)






def loadJobs():
    try:
        data = cur.execute("select * from chatids").fetchall()
    except sqlite3.OperationalError as e:
        logging.critical(f"sqlite3 is not properly set up. Exception: '{str(e)}'")
        logging.critical("run DB_RESET to reset (will delete everything)")
        exit()

    for line in data:
        try:

            localDate = datetime.now().replace(hour=int(line[1]), minute=int(line[2]))
            utcDate = localDate.astimezone(tz=timezone.utc)
            utcTime = time(hour=utcDate.hour, minute=utcDate.minute)
            
            jobReference = application.job_queue.run_daily(callback=callback_heute, time=utcTime, chat_id=int(line[0]))
            
            loadedJobs[int(line[0])] = jobReference

        except Exception as e:
            logging.critical(f"Failed to load jobs from sqlite3. Exception: {str(e)}")
            logging.critical("Manually inspect table 'chatids' for malformed data")


def createMessageStringFromSpider(date, morgen=False):

    message = ""

    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    

    # Heute ist Mo-Fr (fooden)
    if date.isoweekday() <=5:
        dataDate = date
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y") + "_\n\n"

    # Samstag → Plan für übermorgen laden
    elif date.isoweekday() == 6:
        dataDate = date + timedelta(days=2)
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y") 
        if morgen:
            message += "_\n\n"
        else:
            message += " (Übermorgen)_\n\n"
        
    # Sonntag → Plan für morgen laden
    elif date.isoweekday() == 7:
        dataDate = date + timedelta(days=1)
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y")
        
        if morgen:
            message += "_\n\n"
        else:
            message += " (Morgen)_\n\n"


    job = Job(MensaSpider, start_urls=['https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan?location=140' + '&date=' + str(dataDate)])
    processor = Processor(settings=None)  
    data = processor.run(job)

    
    if len(data) == 0 or data[0]['date'].split(",")[1].strip() != dataDate.strftime("%d.%m.%Y"):
        message += "Für diesen Tag existiert noch kein Plan."

    else:
        # generating message from spider results
        for result in data[0]:
            if result == "date":
                continue

            message +=  "*" + result + ":*\n"

            for subitem in data[0][result]:
                message += " •__ " + subitem[0] + "__\n"
                message += "    " +subitem[1] + "\n"

            message += ""
        message += "\n < /heute >  < /morgen >"
        message += "\n < /uebermorgen >"


    

    # required by Markdown V2
    message = message.replace(".", "\.")
    message = message.replace("+", "\+")
    message = message.replace("<", "\<")
    message = message.replace(">", "\>")
    message = message.replace("(", "\(")
    message = message.replace(")", "\)")

    return message


###### crawler setup and stuff
class MensaSpider(scrapy.Spider):
    name = 'mensaplan'


    def parse(self, response):
        result = {}

        result['date'] = response.css('select#edit-date>option[selected="selected"]::text').get()

        for header in response.css('h3.title-prim'):
            name = header.xpath('text()').get()

            result[name] = []

            # for subitem in header.xpath('following-sibling::div/*'):
            for subitem in header.xpath('following-sibling::*'):
                if subitem.attrib == {'class': 'title-prim'}:
                    break
                elif subitem.attrib == {'class': 'accordion u-block'}:
                    for subsubitem in subitem.xpath('child::section'):
                        result[name].append((subsubitem.xpath('header/div/div/h4/text()').get(), subsubitem.xpath('header/div/div/p/text()[2]').get().strip()))

        yield result



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    start_text = """
/subscribe: automatische Nachrichten aktivieren.
/unsubscribe: automatische Nachrichten deaktivieren.
/heute: manuell aktuelles Angebot anzeigen.
/morgen: morgiges Angebot anzeigen.

Wenn /heute oder /morgen kein Wochentag ist, wird der Plan für Montag angezeigt.
    """
    await context.bot.send_message(chat_id=update.effective_chat.id, text=start_text, parse_mode=ParseMode.MARKDOWN)

    await subscribe(update=update, context=context)



async def heute(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = createMessageStringFromSpider(date.today())
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2)


async def morgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(date.today() + timedelta(days=1), morgen=True)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2)


async def uebermorgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(date.today() + timedelta(days=2), morgen=True)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2)


async def dbg(update: Update, context: ContextTypes.DEFAULT_TYPE):

    day = int(context.args[0])
    month = int(context.args[1])
    year = int(context.args[2])
    
    dataDate = date.today().replace(year=year, month=month, day=day)

    message = createMessageStringFromSpider(dataDate)
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2)


async def morgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(date.today() + timedelta(days=1), morgen=True)
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN_V2)



async def changetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id=update.effective_chat.id

    if len(context.args) != 0:
        try:
            hour, min = parseTime(context.args[0])
        except ValueError:
            await context.bot.send_message(chat_id=chat_id, text="Eingegebene Zeit ist ungültig.", parse_mode=ParseMode.MARKDOWN)
            return
    else:
        await context.bot.send_message(chat_id=chat_id, text="Bitte Zeit eingegeben\n( /changetime \[Zeit] )", parse_mode=ParseMode.MARKDOWN)
        return
        
    


    # adding chatid to database (so that job can be recreated @ server restart)
    try:
        unregisterJob(chat_id)
        registerJob(chat_id, hour, min)
        message = "Plan wird ab jetzt automatisch an Wochentagen "+ hour+":"+min + " Uhr gesendet."

    except KeyError:
        message = "Automatische Nachrichten sind noch nicht aktiviert.\n/subscribe oder\n/subscribe \[Zeit] ausführen"


    # confirmation message
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)


# checks time string for validity, and if successful, returns ints Hours, Mins
def parseTime(strTime):
    regex = "([01]?[0-9]|2[0-3]):[0-5][0-9]";
    cregex = re.compile(regex)

    m = re.match(cregex, strTime)

    if m is not None:
        return m.group().split(":")
    else:
        raise ValueError



async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id=update.effective_chat.id

    if len(context.args) != 0:
        try:
            hour, min = parseTime(context.args[0])
        except ValueError:
            await context.bot.send_message(chat_id=chat_id, text="Eingegebene Zeit ist ungültig!", parse_mode=ParseMode.MARKDOWN)
            return
    else:
        hour, min = ("6", "00")
    


    # adding chatid to database (so that job can be recreated @ server restart)
    try:
        registerJob(chat_id, hour, min)
        message = "Plan wird ab jetzt automatisch an Wochentagen "+ hour+":"+min + " Uhr gesendet.\n\n/changetime \[Zeit] zum Ändern\n/unsubscribe zum Deaktivieren"

    except sqlite3.IntegrityError:
        message = "Automatische Nachrichten sind schon aktiviert.\n(Zum Ändern der Zeit: /changetime \[Zeit])"


    # confirmation message
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)




async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id=update.effective_chat.id

    try:
        unregisterJob(chat_id)
        # confirmation message
        await context.bot.send_message(chat_id=chat_id, text="Plan wird nicht mehr automatisch gesendet.", parse_mode=ParseMode.MARKDOWN)

    except KeyError:
        await context.bot.send_message(chat_id=chat_id, text="Automatische Nachrichten waren bereits deaktiviert.", parse_mode=ParseMode.MARKDOWN)


# used as callback when called automatically (daily)
async def callback_heute(context):
    job = context.job
    message = createMessageStringFromSpider(date.today())

    await context.bot.send_message(job.chat_id, text=message, parse_mode=ParseMode.MARKDOWN_V2)



if __name__ == '__main__':

    # if pidfile exists ≙ program is already running: catch the pidfilelocked exc, exit()vi
    try:
        # prevents multiple instances of this script to run at the same time → easy way to restart in case of error
        with PidFile():

            try:
                with open("token.txt", "r") as fobj:
                    token = fobj.readline().strip()
            except FileNotFoundError:
                logging.critical("'token.txt' missing. Create it and insert the token (without quotation marks)")
                exit()


            application = ApplicationBuilder().token(token).read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30).build()
            
            # restoring all daily auto messages using chatids and times saved to jobs.db
            loadJobs()

            dbg_handler = CommandHandler('dbg', dbg)
            application.add_handler(dbg_handler)
            
            start_handler = CommandHandler('start', start)
            application.add_handler(start_handler)

            heute_handler = CommandHandler('heute', heute)
            application.add_handler(heute_handler)

            morgen_handler = CommandHandler('morgen', morgen)
            application.add_handler(morgen_handler)

            uebermorgen_handler = CommandHandler('uebermorgen', uebermorgen)
            application.add_handler(uebermorgen_handler)

            ubermorgen_handler = CommandHandler('ubermorgen', uebermorgen)
            application.add_handler(ubermorgen_handler)

            subscribe_handler = CommandHandler('subscribe', subscribe)
            application.add_handler(subscribe_handler)

            unsubscribe_handler = CommandHandler('unsubscribe', unsubscribe)
            application.add_handler(unsubscribe_handler)

            changetime_handler = CommandHandler('changetime',  changetime)
            application.add_handler(changetime_handler)
            
            
            application.run_polling()

    except PidFileAlreadyLockedError:
        exit()