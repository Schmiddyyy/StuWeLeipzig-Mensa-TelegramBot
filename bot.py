import logging
from multiprocessing import context

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

import scrapy
from scrapyscript import Job, Processor


from datetime import date #, datetime, timedelta

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)



###### crawler instantiation und stuff
class MensaSpider(scrapy.Spider):
    name = 'quotes'
    # start_urls = [
    #     'https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan?location=140',
    # ]

    def parse(self, response):

        #date = response.css('#edit-date * ::text').get()

        # for meal in response.css('div.meals__summary'):
        #     yield {
        #         'date': date,
        #         'name': meal.xpath('h4/text()').get(),
        #         'preis': meal.xpath('p/text()[2]').get().strip()
        #     }

        for meal in response.css('section.accordion__item'):         
            yield {
                'name': meal.xpath('header/div/div/h4/text()').get(),
                'preis': meal.xpath('header/div/div/p/text()[2]').get().strip(),
                'additional': meal.xpath('details/ul/li/text()').getall()
            }



processor = Processor(settings=None)



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    message = ""

    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    
    current_date = date.today()

    # Heute ist Mo-Fr (fooden)
    if current_date.isoweekday() <=5:
        message += weekdays[current_date.isoweekday()-1] + date.today().strftime(", %d.%m.%Y") + " (Heute)\n\n"

        todayjob = Job(MensaSpider, start_urls=['https://www.studentenwerk-leipzig.de/mensen-cafeterien/speiseplan?location=140' + '&date=' + str(date.today())])
        
        # spider für heute
        data = processor.run(todayjob)

        # getting spider results and generating message
        for gericht in data:
            message += "*" + gericht['name'] + "* \n" 

            for additional in gericht['additional']:
                message += "+" + additional + "\n"

            message += gericht['preis'] + "\n\n"
            

        await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode=ParseMode.MARKDOWN)



    

    

    #  # n ächster Tag ist ein Wochentag.
    # if current_weekday < 5 or current_weekday == 7:
    #     next_date = date.today() + timedelta(days=1)
    # # es ist Freitag → Montag in 3 Tagen
    # elif date.today().isoweekday() == 5:
    #     next_date = date.today() + timedelta(days=3)
    # # Samstag → Montag in 2 Tagen
    # elif date.today().isoweekday() == 6:
    #     next _date = date.today() + timedelta(days=2)

    # datum als param????

    # nextjob = Job(MensaSpider, url='')
    # data = processor.run(job)
    

    








if __name__ == '__main__':
    application = ApplicationBuilder().token('TOKEN').build()
    
    start_handler = CommandHandler('start', start)



    application.add_handler(start_handler)
    
    application.run_polling()