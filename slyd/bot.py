"""
Bot resource

Defines bot/fetch endpoint, e.g.:
    curl -d '{"request": {"url": "http://scrapinghub.com/"}}' http://localhost:9001/bot/fetch

The "request" is an object whose fields match the parameters of a Scrapy
request:
    http://doc.scrapy.org/en/latest/topics/request-response.html#scrapy.http.Request

Returns a json object. If there is an "error" field, that holds the request
error to display. Otherwise you will find the following fields:
    * page -- the retrieved page - will be annotated in future

"""
import json
from functools import partial
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from scrapy.http import Request
from scrapy.spider import BaseSpider
from scrapy import signals, log
from scrapy.crawler import Crawler
from scrapy.http import HtmlResponse
from scrapy.exceptions import DontCloseSpider
from slybot.utils import htmlpage_from_response
from slybot.spider import IblSpider


def create_bot_resource(settings, spec_manager):
    bot = Bot(settings, spec_manager)
    bot.putChild('fetch', Fetch(bot))
    return bot


class Bot(Resource):
    spider = BaseSpider('slyd')

    def __init__(self, settings, spec_manager):
        # twisted base class is old-style so we cannot user super()
        Resource.__init__(self)
        self.spec_manager = spec_manager
        # initialize scrapy crawler
        crawler = Crawler(settings)
        crawler.configure()
        crawler.signals.connect(self.keep_spider_alive, signals.spider_idle)
        crawler.crawl(self.spider)
        crawler.start()

        self.crawler = crawler
        log.msg("bot initialized", level=log.DEBUG)

    def keep_spider_alive(self, spider):
        raise DontCloseSpider("keeping it open")


class BotResource(Resource):
    def __init__(self, bot):
        Resource.__init__(self)
        self.bot = bot


class Fetch(BotResource):

    def render_POST(self, request):
        #TODO: validate input data, handle errors, etc.
        params = read_json(request)
        scrapy_request_kwargs = params['request']
        scrapy_request_kwargs.update(
            callback=self.fetch_callback,
            errback=partial(self.fetch_errback, request),
            dont_filter=True,  # TODO: disable duplicate middleware
            meta=dict(
                handle_httpstatus_all=True,
                twisted_request=request,
                slyd_request_params=params
            )
        )
        request = Request(**scrapy_request_kwargs)
        self.bot.crawler.engine.schedule(request, self.bot.spider)
        return NOT_DONE_YET

    def fetch_callback(self, response):
        if response.status != 200:
            write_json(response, error="Received http %s" % response.status)
        if not isinstance(response, HtmlResponse):
            msg = "Non-html response: %s" % response.headers.get(
                'content-type', 'no content type')
            write_json(response, error=msg)
        try:
            params = response.meta['slyd_request_params']
            result = dict(page=response.body_as_unicode())
            spider = self.create_spider(params)
            if spider is not None:
                htmlpage = htmlpage_from_response(response)
                items, _link_regions = spider.extract_items(htmlpage)
                result['items'] = [i._values for i in items]
            write_json(response, **result)
        except Exception as ex:
            log.err()
            write_json(response, error="unexpected internal error: %s" % ex)

    def create_spider(self, params, **kwargs):
        project = params['project']
        spider = params['spider']
        specs = self.bot.spec_manager.load_spec(project)
        try:
            spec = specs['spiders'][spider]
            items = specs['items']
            extractors = specs['extractors']
            return IblSpider(spider, spec, items, extractors,
                **kwargs)
        except KeyError as ex:
            log.msg("not extracting, missing spec for %s" % ex.message,
                level=log.DEBUG)

    def fetch_errback(self, twisted_request, failure):
        msg = "unexpected error response: %s" % failure
        log.msg(msg, level=log.ERROR)
        finish_request(twisted_request, error=msg)


def read_json(request):
    data = request.content.getvalue()
    return json.loads(data)


def write_json(response, **resp_obj):
    request = response.meta['twisted_request']
    finish_request(request, **resp_obj)

def finish_request(trequest, **resp_obj):
    jdata = json.dumps(resp_obj)
    trequest.setHeader('Content-Type', 'application/json')
    trequest.setHeader('Content-Length', len(jdata))
    trequest.write(jdata)
    trequest.finish()
