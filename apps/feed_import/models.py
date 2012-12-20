import datetime
import mongoengine as mongo
import httplib2
import pickle
import base64
from collections import defaultdict
from StringIO import StringIO
from xml.etree.ElementTree import Element, SubElement, Comment, tostring
from lxml import etree
from django.db import models
from django.contrib.auth.models import User
from mongoengine.queryset import OperationError
import vendor.opml as opml
from apps.rss_feeds.models import Feed, DuplicateFeed, MStarredStory
from apps.reader.models import UserSubscription, UserSubscriptionFolders
from utils import json_functions as json, urlnorm
from utils import log as logging
from utils.feed_functions import timelimit

from south.modelsinspector import add_introspection_rules
add_introspection_rules([], ["^oauth2client\.django_orm\.FlowField"])
add_introspection_rules([], ["^oauth2client\.django_orm\.CredentialsField"])


class OAuthToken(models.Model):
    user = models.OneToOneField(User, null=True, blank=True)
    session_id = models.CharField(max_length=50, null=True, blank=True)
    uuid = models.CharField(max_length=50, null=True, blank=True)
    remote_ip = models.CharField(max_length=50, null=True, blank=True)
    request_token = models.CharField(max_length=50)
    request_token_secret = models.CharField(max_length=50)
    access_token = models.CharField(max_length=50)
    access_token_secret = models.CharField(max_length=50)
    credential = models.TextField(null=True, blank=True)
    created_date = models.DateTimeField(default=datetime.datetime.now)
    
    
class OPMLExporter:
    
    def __init__(self, user):
        self.user = user
        self.fetch_feeds()
        
    def process(self, verbose=False):
        now = str(datetime.datetime.now())

        root = Element('opml')
        root.set('version', '1.1')
        root.append(Comment('Generated by NewsBlur - www.newsblur.com'))

        head       = SubElement(root, 'head')
        title      = SubElement(head, 'title')
        title.text = 'NewsBlur Feeds'
        dc         = SubElement(head, 'dateCreated')
        dc.text    = now
        dm         = SubElement(head, 'dateModified')
        dm.text    = now
        folders    = self.get_folders()
        body       = SubElement(root, 'body')
        self.process_outline(body, folders, verbose=verbose)
        return tostring(root)
        
    def process_outline(self, body, folders, verbose=False):
        for obj in folders:
            if isinstance(obj, int) and obj in self.feeds:
                feed = self.feeds[obj]
                if verbose:
                    print "     ---> Adding feed: %s - %s" % (feed['id'],
                                                              feed['feed_title'][:30])
                feed_attrs = self.make_feed_row(feed)
                body.append(Element('outline', feed_attrs))
            elif isinstance(obj, dict):
                for folder_title, folder_objs in obj.items():
                    if verbose:
                        print " ---> Adding folder: %s" % folder_title
                    folder_element = Element('outline', {'text': folder_title, 'title': folder_title})
                    body.append(self.process_outline(folder_element, folder_objs, verbose=verbose))
        return body
    
    def make_feed_row(self, feed):
        feed_attrs = {
            'text': feed['feed_title'],
            'title': feed['feed_title'],
            'type': 'rss',
            'version': 'RSS',
            'htmlUrl': feed['feed_link'] or "",
            'xmlUrl': feed['feed_address'] or "",
        }
        return feed_attrs
        
    def get_folders(self):
        folders = UserSubscriptionFolders.objects.get(user=self.user)
        return json.decode(folders.folders)
        
    def fetch_feeds(self):
        subs = UserSubscription.objects.filter(user=self.user)
        self.feeds = dict((sub.feed_id, sub.canonical()) for sub in subs)
        

class Importer:

    def clear_feeds(self):
        UserSubscription.objects.filter(user=self.user).delete()

    def clear_folders(self):
        UserSubscriptionFolders.objects.filter(user=self.user).delete()

    
class OPMLImporter(Importer):
    
    def __init__(self, opml_xml, user):
        self.user = user
        self.opml_xml = opml_xml
    
    def try_processing(self):
        folders = timelimit(20)(self.process)()
        return folders
        
    def process(self):
        self.clear_feeds()
        outline = opml.from_string(str(self.opml_xml))
        folders = self.process_outline(outline)
        self.clear_folders()
        UserSubscriptionFolders.objects.create(user=self.user, folders=json.encode(folders))
        
        return folders
        
    def process_outline(self, outline):
        folders = []
        for item in outline:
            if (not hasattr(item, 'xmlUrl') and 
                (hasattr(item, 'text') or hasattr(item, 'title'))):
                folder = item
                title = getattr(item, 'text', None) or getattr(item, 'title', None)
                # if hasattr(folder, 'text'):
                #     logging.info(' ---> [%s] ~FRNew Folder: %s' % (self.user, folder.text))
                folders.append({title: self.process_outline(folder)})
            elif hasattr(item, 'xmlUrl'):
                feed = item
                if not hasattr(feed, 'htmlUrl'):
                    setattr(feed, 'htmlUrl', None)
                # If feed title matches what's in the DB, don't override it on subscription.
                feed_title = getattr(feed, 'title', None) or getattr(feed, 'text', None)
                if not feed_title:
                    setattr(feed, 'title', feed.htmlUrl or feed.xmlUrl)
                    user_feed_title = None
                else:
                    setattr(feed, 'title', feed_title)
                    user_feed_title = feed.title

                feed_address = urlnorm.normalize(feed.xmlUrl)
                feed_link = urlnorm.normalize(feed.htmlUrl)
                if len(feed_address) > Feed._meta.get_field('feed_address').max_length:
                    continue
                if feed_link and len(feed_link) > Feed._meta.get_field('feed_link').max_length:
                    continue
                # logging.info(' ---> \t~FR%s - %s - %s' % (feed.title, feed_link, feed_address,))
                feed_data = dict(feed_address=feed_address, feed_link=feed_link, feed_title=feed.title)
                # feeds.append(feed_data)

                # See if it exists as a duplicate first
                duplicate_feed = DuplicateFeed.objects.filter(duplicate_address=feed_address)
                if duplicate_feed:
                    feed_db = duplicate_feed[0].feed
                else:
                    feed_data['active_subscribers'] = 1
                    feed_data['num_subscribers'] = 1
                    feed_db, _ = Feed.find_or_create(feed_address=feed_address, 
                                                     feed_link=feed_link,
                                                     defaults=dict(**feed_data))

                if user_feed_title == feed_db.feed_title:
                    user_feed_title = None
                
                us, _ = UserSubscription.objects.get_or_create(
                    feed=feed_db, 
                    user=self.user,
                    defaults={
                        'needs_unread_recalc': True,
                        'mark_read_date': datetime.datetime.utcnow() - datetime.timedelta(days=1),
                        'active': self.user.profile.is_premium,
                        'user_title': user_feed_title
                    }
                )
                if self.user.profile.is_premium and not us.active:
                    us.active = True
                    us.save()
                if not us.needs_unread_recalc:
                    us.needs_unread_recalc = True
                    us.save()
                if feed_db.pk not in folders:
                    folders.append(feed_db.pk)

        return folders
    
    def count_feeds_in_opml(self):
        opml_count = len(opml.from_string(self.opml_xml))
        sub_count = UserSubscription.objects.filter(user=self.user).count()
        return max(sub_count, opml_count)
        

class UploadedOPML(mongo.Document):
    user_id = mongo.IntField()
    opml_file = mongo.StringField()
    upload_date = mongo.DateTimeField(default=datetime.datetime.now)
    
    def __unicode__(self):
        user = User.objects.get(pk=self.user_id)
        return "%s: %s characters" % (user.username, len(self.opml_file))
    
    meta = {
        'collection': 'uploaded_opml',
        'allow_inheritance': False,
        'order': '-upload_date',
        'indexes': ['user_id', '-upload_date'],
    }
    

class GoogleReaderImporter(Importer):
    
    def __init__(self, user, xml=None):
        self.user = user
        self.subscription_folders = []
        self.scope = "http://www.google.com/reader/api"
        self.xml = xml
        self.auto_active = False
    
    def import_feeds(self, auto_active=False):
        self.auto_active = auto_active
        sub_url = "%s/0/subscription/list" % self.scope
        if not self.xml:
            feeds_xml = self.send_request(sub_url)
        else:
            feeds_xml = self.xml
        if feeds_xml:
            self.process_feeds(feeds_xml)
        
    def send_request(self, url):
        user_tokens = OAuthToken.objects.filter(user=self.user)

        if user_tokens.count():
            user_token = user_tokens[0]
            if user_token.credential:
                credential = pickle.loads(base64.b64decode(user_token.credential))
                http = httplib2.Http()
                http = credential.authorize(http)
                content = http.request(url)
                return content and content[1]
        
    def process_feeds(self, feeds_xml):
        self.clear_feeds()
        self.feeds = self.parse(feeds_xml)

        folders = defaultdict(list)
        for item in self.feeds:
            folders = self.process_item(item, folders)

        folders = self.rearrange_folders(folders)
        logging.user(self.user, "~BB~FW~SBGoogle Reader import: ~BT~FW%s" % (self.subscription_folders))
        
        self.clear_folders()
        UserSubscriptionFolders.objects.get_or_create(user=self.user, defaults=dict(
                                                      folders=json.encode(self.subscription_folders)))

    def parse(self, feeds_xml):
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(StringIO(feeds_xml), parser)
        feeds = tree.xpath('/object/list/object')
        return feeds
    
    def process_item(self, item, folders):
        feed_title = item.xpath('./string[@name="title"]') and \
                        item.xpath('./string[@name="title"]')[0].text
        feed_address = item.xpath('./string[@name="id"]') and \
                        item.xpath('./string[@name="id"]')[0].text.replace('feed/', '')
        feed_link = item.xpath('./string[@name="htmlUrl"]') and \
                        item.xpath('./string[@name="htmlUrl"]')[0].text
        category = item.xpath('./list[@name="categories"]/object/string[@name="label"]') and \
                        item.xpath('./list[@name="categories"]/object/string[@name="label"]')[0].text
        
        if not feed_address:
            feed_address = feed_link
        
        try:
            feed_link = urlnorm.normalize(feed_link)
            feed_address = urlnorm.normalize(feed_address)

            if len(feed_address) > Feed._meta.get_field('feed_address').max_length:
                return folders

            # See if it exists as a duplicate first
            duplicate_feed = DuplicateFeed.objects.filter(duplicate_address=feed_address)
            if duplicate_feed:
                feed_db = duplicate_feed[0].feed
            else:
                feed_data = dict(feed_title=feed_title)
                feed_data['active_subscribers'] = 1
                feed_data['num_subscribers'] = 1
                feed_db, _ = Feed.find_or_create(feed_address=feed_address, feed_link=feed_link,
                                                 defaults=dict(**feed_data))

            us, _ = UserSubscription.objects.get_or_create(
                feed=feed_db, 
                user=self.user,
                defaults={
                    'needs_unread_recalc': True,
                    'mark_read_date': datetime.datetime.utcnow() - datetime.timedelta(days=1),
                    'active': self.user.profile.is_premium or self.auto_active,
                }
            )
            if not us.needs_unread_recalc:
                us.needs_unread_recalc = True
                us.save()
            if not category: category = "Root"
            if feed_db.pk not in folders[category]:
                folders[category].append(feed_db.pk)
        except Exception, e:
            logging.info(' *** -> Exception: %s: %s' % (e, item))

        return folders
        
    def rearrange_folders(self, folders, depth=0):
        for folder, items in folders.items():
            if folder == 'Root':
                self.subscription_folders += items
            else:
                # folder_parents = folder.split(u' \u2014 ')
                self.subscription_folders.append({folder: items})
    
    def import_starred_items(self, count=10):
        sub_url = "%s/0/stream/contents/user/-/state/com.google/starred?n=%s" % (self.scope, count)
        stories_str = self.send_request(sub_url)
        try:
            stories = json.decode(stories_str)
        except:
            logging.user(self.user, "~BB~FW~SBGoogle Reader starred stories: ~BT~FWNo stories")
            stories = None
        if stories:
            logging.user(self.user, "~BB~FW~SBGoogle Reader starred stories: ~BT~FW%s stories" % (len(stories['items'])))
            self.process_starred_items(stories['items'])
        
    def process_starred_items(self, stories):
        for story in stories:
            try:
                original_feed = Feed.get_feed_from_url(story['origin']['streamId'], create=False, fetch=False)
                if not original_feed:
                    original_feed = Feed.get_feed_from_url(story['origin']['htmlUrl'], create=False, fetch=False)
                content = story.get('content') or story.get('summary')
                story_db = {
                    "user_id": self.user.pk,
                    "starred_date": datetime.datetime.fromtimestamp(story['updated']),
                    "story_date": datetime.datetime.fromtimestamp(story['published']),
                    "story_title": story.get('title', story.get('origin', {}).get('title', '[Untitled]')),
                    "story_permalink": story['alternate'][0]['href'],
                    "story_guid": story['id'],
                    "story_content": content.get('content'),
                    "story_author_name": story.get('author'),
                    "story_feed_id": original_feed and original_feed.pk,
                    "story_tags": [tag for tag in story.get('categories', []) if 'user/' not in tag]
                }
                logging.user(self.user, "~FCStarring: ~SB%s~SN in ~SB%s" % (story_db['story_title'][:50], original_feed and original_feed))
                MStarredStory.objects.create(**story_db)
            except OperationError:
                logging.user(self.user, "~FCAlready starred: ~SB%s" % (story_db['story_title'][:50]))
            except Exception, e:
                logging.user(self.user, "~FC~BRFailed to star: ~SB%s / %s" % (story, e))
                