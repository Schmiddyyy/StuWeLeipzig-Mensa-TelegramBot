from pid import PidFile


import os
import atexit

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



# # clearing the log
# with open('log.log', 'w'):
#     pass

# # logger init
# mylogger = logging.getLogger('myLogger')


# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


# # logger to ./log.txt
# fileHandler = logging.FileHandler(filename='log.log')
# fileHandler.setLevel(logging.INFO)
# fileHandler.setFormatter(formatter)
# mylogger.addHandler(fileHandler)

# # logger to stdout
# stdOutHandler = logging.StreamHandler(stream=sys.stdout)
# stdOutHandler.setLevel(logging.INFO)
# stdOutHandler.setFormatter(formatter)
# mylogger.addHandler(stdOutHandler)


# # 'spoofing' class to redirect stdout to myLogger (which then forwards it to stdout AND log.log)
# class LoggerWriter:
#     def __init__(self, level):
#         self.level = level

#     def write(self, message):

#         if message != '\n':
#             self.level(message)

#     def flush(self):
#         # flush clears the line buffer (happens when writing \n), which does not matter here
#         pass

# # sys.stdout = LoggerWriter(mylogger.debug)
# sys.stderr = LoggerWriter(mylogger.warning)






loadedJobs = {}

# When True, automatic messages will be sent every 10 seconds instead of daily.
# WARNING: this applies to everyone, so everyone subscribed to messages will get a msg every 10 seconds
DBG = False



def registerJob(id, hour, min):
    current_registration = cur.execute("select * from chatids where id= (?) ", [id]).fetchall()
    
    
    # if len(current_registration) == 1:
    #     if current_registration[0][1] == hour and current_registration[0][2] == min:
    #         print("no change....")
    #         print(hour)
    #         print(min)
    #         #exit()
    #     else:
    #         print("NEW TIME")

    # print(current_registration)

    #exit()

    cur.execute("insert into chatids values(?,?,?)", [id, hour, min])
    con.commit()


    # starting the job
    if DBG:
        jobReference = application.job_queue.run_repeating(callback=callback_heute, interval=timedelta(seconds=5), chat_id=id)
    else:
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
            if DBG:
                jobReference = application.job_queue.run_repeating(callback=callback_heute, interval=timedelta(seconds=5), chat_id=int(line[0]))
            else:
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

    
    if len(data) == 0:
        message += "*Für diesen Tag existiert noch kein Plan.*"

    else:
        # generating message from spider results
        for gericht in data:
            message += "*" + gericht['name'] + "* \n" 

            for additional in gericht['additional']:
                message += " + " + additional + "\n"

            message += gericht['preis'] + "\n\n"
        
    message += "  < /heute >      < /morgen >"

    return message


###### crawler setup and stuff
class MensaSpider(scrapy.Spider):
    name = 'mensaplan'

    # the URL has to be passed when creating a Job

    def parse(self, response):

        for meal in response.css('section.accordion__item'):         
            yield {
                'name': meal.xpath('header/div/div/h4/text()').get(),
                'preis': meal.xpath('header/div/div/p/text()[2]').get().strip(),
                'additional': meal.xpath('details/ul/li/text()').getall()
            }



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
    # alarm: update: Update, context: ContextTypes.DEFAULT_TYPE
    
    # aufruf: 'context' fehlt
    
    message = createMessageStringFromSpider(date.today())
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN)


async def morgen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = createMessageStringFromSpider(date.today() + timedelta(days=1), morgen=True)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN)


async def changetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    chat_id=update.effective_chat.id

    if len(context.args) != 0:
        try:
            hour, min = parseTime(context.args[0])
        except ValueError:
            await context.bot.send_message(chat_id=chat_id, text="Eingegebene Zeit ist ungültig!", parse_mode=ParseMode.MARKDOWN)
            return
    else:
        await context.bot.send_message(chat_id=chat_id, text="Es wurde keine Zeit eingegeben.", parse_mode=ParseMode.MARKDOWN)
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

    await context.bot.send_message(job.chat_id, text=message, parse_mode=ParseMode.MARKDOWN)



if __name__ == '__main__':

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


        
        start_handler = CommandHandler('start', start)
        application.add_handler(start_handler)

        heute_handler = CommandHandler('heute', heute)
        application.add_handler(heute_handler)

        morgen_handler = CommandHandler('morgen', morgen)
        application.add_handler(morgen_handler)

        subscribe_handler = CommandHandler('subscribe', subscribe)
        application.add_handler(subscribe_handler)

        unsubscribe_handler = CommandHandler('unsubscribe', unsubscribe)
        application.add_handler(unsubscribe_handler)

        changetime_handler = CommandHandler('changetime',  changetime)
        application.add_handler(changetime_handler)
        
        
        application.run_polling()


    
    # while True:
    #     try:
    #         application.run_polling()
    #     except RuntimeError as e:
    #         logging.info(f'EventLoop was stopped: '{str(e)})
    #         exit()