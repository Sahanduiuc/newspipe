#! /usr/bin/env python
#-*- coding: utf-8 -*-

# JARR - A Web based news aggregator.
# Copyright (C) 2010-2016  Cédric Bonhomme - https://www.cedricbonhomme.org
#
# For more information : https://github.com/JARR/JARR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

__author__ = "Cedric Bonhomme"
__version__ = "$Revision: 1.8 $"
__date__ = "$Date: 2010/12/07 $"
__revision__ = "$Date: 2016/04/10 $"
__copyright__ = "Copyright (c) Cedric Bonhomme"
__license__ = "AGPLv3"

#
# This file provides functions used for:
# - import from a JSON file;
# - generation of tags cloud;
# - HTML processing.
#

import re
import sys
import glob
import opml
import json
import logging
import datetime
import operator
import urllib
import subprocess
import sqlalchemy
try:
    from urlparse import urlparse, parse_qs, urlunparse
except:
    from urllib.parse import urlparse, parse_qs, urlunparse, urljoin
from bs4 import BeautifulSoup
from collections import Counter
from contextlib import contextmanager
from flask import request

import conf
from bootstrap import db
from web import controllers
from web.models import User, Feed, Article
from web.lib.utils import clear_string

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = set(['xml', 'opml', 'json'])

def is_safe_url(target):
    """
    Ensures that a redirect target will lead to the same server.
    """
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and \
           ref_url.netloc == test_url.netloc

def get_redirect_target():
    """
    Looks at various hints to find the redirect target.
    """
    for target in request.args.get('next'), request.referrer:
        if not target:
            continue
        if is_safe_url(target):
            return target

def allowed_file(filename):
    """
    Check if the uploaded file is allowed.
    """
    return '.' in filename and \
            filename.rsplit('.', 1)[1] in ALLOWED_EXTENSIONS

@contextmanager
def opened_w_error(filename, mode="r"):
    try:
        f = open(filename, mode)
    except IOError as err:
        yield None, err
    else:
        try:
            yield f, None
        finally:
            f.close()

def fetch(id, feed_id=None):
    """
    Fetch the feeds in a new processus.
    The "asyncio" crawler is launched with the manager.
    """
    cmd = [sys.executable, conf.BASE_DIR + '/manager.py', 'fetch_asyncio',
           str(id), str(feed_id)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)

def history(user_id, year=None, month=None):
    """
    Sort articles by year and month.
    """
    articles_counter = Counter()
    articles = controllers.ArticleController(user_id).read()
    if None != year:
        articles = articles.filter(sqlalchemy.extract('year', Article.date) == year)
        if None != month:
            articles = articles.filter(sqlalchemy.extract('month', Article.date) == month)
    for article in articles.all():
        if None != year:
            articles_counter[article.date.month] += 1
        else:
            articles_counter[article.date.year] += 1
    return articles_counter, articles

def import_opml(email, opml_content):
    """
    Import new feeds from an OPML file.
    """
    user = User.query.filter(User.email == email).first()
    try:
        subscriptions = opml.from_string(opml_content)
    except:
        logger.exception("Parsing OPML file failed:")
        raise

    def read(subsubscription, nb=0):
        """
        Parse recursively through the categories and sub-categories.
        """
        for subscription in subsubscription:
            if len(subscription) != 0:
                nb = read(subscription, nb)
            else:
                try:
                    title = subscription.text
                except:
                    title = ""
                try:
                    description = subscription.description
                except:
                    description = ""
                try:
                    link = subscription.xmlUrl
                except:
                    continue
                if None != Feed.query.filter(Feed.user_id == user.id, Feed.link == link).first():
                    continue
                try:
                    site_link = subscription.htmlUrl
                except:
                    site_link = ""
                new_feed = Feed(title=title, description=description,
                                link=link, site_link=site_link,
                                enabled=True)
                user.feeds.append(new_feed)
                nb += 1
        return nb
    nb = read(subscriptions)
    db.session.commit()
    return nb

def import_json(email, json_content):
    """
    Import an account from a JSON file.
    """
    user = User.query.filter(User.email == email).first()
    json_account = json.loads(json_content)
    nb_feeds, nb_articles = 0, 0
    # Create feeds:
    for feed in json_account["result"]:
        if None != Feed.query.filter(Feed.user_id == user.id,
                                    Feed.link == feed["link"]).first():
            continue
        new_feed = Feed(title=feed["title"],
                        description="",
                        link=feed["link"],
                        site_link=feed["site_link"],
                        created_date=datetime.datetime.
                            fromtimestamp(int(feed["created_date"])),
                        enabled=feed["enabled"])
        user.feeds.append(new_feed)
        nb_feeds += 1
    db.session.commit()
    # Create articles:
    for feed in json_account["result"]:
        user_feed = Feed.query.filter(Feed.user_id == user.id,
                                        Feed.link == feed["link"]).first()
        if None != user_feed:
            for article in feed["articles"]:
                if None == Article.query.filter(Article.user_id == user.id,
                                    Article.feed_id == user_feed.id,
                                    Article.link == article["link"]).first():
                    new_article = Article(link=article["link"],
                                title=article["title"],
                                content=article["content"],
                                readed=article["readed"],
                                like=article["like"],
                                retrieved_date=datetime.datetime.
                                    fromtimestamp(int(article["retrieved_date"])),
                                date=datetime.datetime.
                                    fromtimestamp(int(article["date"])),
                                user_id=user.id,
                                feed_id=user_feed.id)
                    user_feed.articles.append(new_article)
                    nb_articles += 1
    db.session.commit()
    return nb_feeds, nb_articles

def clean_url(url):
    """
    Remove utm_* parameters
    """
    parsed_url = urlparse(url)
    qd = parse_qs(parsed_url.query, keep_blank_values=True)
    filtered = dict((k, v) for k, v in qd.items()
                                        if not k.startswith('utm_'))
    return urlunparse([
        parsed_url.scheme,
        parsed_url.netloc,
        urllib.parse.quote(urllib.parse.unquote(parsed_url.path)),
        parsed_url.params,
        urllib.parse.urlencode(filtered, doseq=True),
        parsed_url.fragment
    ]).rstrip('=')

def load_stop_words():
    """
    Load the stop words and return them in a list.
    """
    stop_words_lists = glob.glob('./JARR/var/stop_words/*.txt')
    stop_words = []

    for stop_wods_list in stop_words_lists:
        with opened_w_error(stop_wods_list, "r") as (stop_wods_file, err):
            if err:
                stop_words = []
            else:
                stop_words += stop_wods_file.read().split(";")
    return stop_words

def top_words(articles, n=10, size=5):
    """
    Return the n most frequent words in a list.
    """
    stop_words = load_stop_words()
    words = Counter()
    wordre = re.compile(r'\b\w{%s,}\b' % size, re.I)
    for article in articles:
        for word in [elem.lower() for elem in
                wordre.findall(clear_string(article.content)) \
                if elem.lower() not in stop_words]:
            words[word] += 1
    return words.most_common(n)

def tag_cloud(tags):
    """
    Generates a tags cloud.
    """
    tags.sort(key=operator.itemgetter(0))
    return '\n'.join([('<font size=%d><a href="/search?query=%s" title="Count: %s">%s</a></font>' % \
                    (min(1 + count * 7 / max([tag[1] for tag in tags]), 7), word, format(count, ',d'), word)) \
                        for (word, count) in tags])

if __name__ == "__main__":
    import_opml("root@jarr.localhost", "./var/feeds_test.opml")
    #import_opml("root@jarr.localhost", "./var/JARR.opml")