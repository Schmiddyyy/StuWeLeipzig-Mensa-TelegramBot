import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

import scrapy
from scrapyscript import Job, Processor


from datetime import date, timedelta

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


###### crawler instantiation und stuff
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
    
    message = ""

    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    
    currentDate = date.today()#+timedelta(days=14)
                                # for testing purposes

    # Heute ist Mo-Fr (fooden)
    if currentDate.isoweekday() <=5:
        dataDate = currentDate
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y") + " (Heute)_\n\n"

    # Samstag → Plan für übermorgen laden
    elif currentDate.isoweekday() == 6:
        dataDate = currentDate + timedelta(days=2)
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y") + " (Übermorgen)_\n\n"
        
    # Sonntag → Plan für morgen laden
    elif currentDate.isoweekkday() == 7:
        dataDate = currentDate + timedelta(days=1)
        message += "_" + weekdays[dataDate.isoweekday()-1] + dataDate.strftime(", %d.%m.%Y") + " (Morgen)_\n\n"


    job = Job(MensaSpider, start_urls=['https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan?location=140' + '&date=' + str(dataDate)])
    processor = Processor(settings=None)  
    data = processor.run(job)

    
    if len(data) == 0:
        message += "*Für diesen Tag existiert noch kein Plan.*"

    else:
        # getting spider results and generating message
        for gericht in data:
            message += "*" + gericht['name'] + "* \n" 

            for additional in gericht['additional']:
                message += " + " + additional + "\n"

            message += gericht['preis'] + "\n\n"
        

    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN)


if __name__ == '__main__':
    application = ApplicationBuilder().token('TOKEN').build()
    
    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)
    application.run_polling()