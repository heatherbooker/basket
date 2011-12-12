from functools import wraps
from datetime import date
import urlparse
import json
import uuid

from django.http import (HttpResponse, HttpResponseRedirect,
                         HttpResponseBadRequest, HttpResponseForbidden)
from django.views.decorators.csrf import csrf_exempt 
from django.conf import settings

from models import Subscriber
from responsys import Responsys, NewsletterException, UnauthorizedException

NEWSLETTERS = {
    'mozilla-and-you': 'MOZILLA_AND_YOU',
    'mobile': 'ABOUT_MOBILE',
    'beta': 'FIREFOX_BETA_NEWS',
    'aurora': 'AURORA',
    'about-mozilla': 'ABOUT_MOZILLA',
    'drumbeat': 'DRUMBEAT_NEWS_GROUP',
    'addons': 'ABOUT_ADDONS',
    'hacks': 'ABOUT_HACKS',
    'labs': 'ABOUT_LABS',
    'qa-news': 'QA_NEWS',
    'student-reps': 'STUDENT_REPS',
    'about-standards': 'ABOUT_STANDARDS',
    'mobile-addon-dev': 'MOBILE_ADDON_DEV',
    'addon-dev': 'ADD_ONS',
    'join-mozilla': 'JOIN_MOZILLA',
    'mozilla-phone': 'MOZILLA_PHONE',
    'app-dev': 'APP_DEV',
    'moz-spaces', 'MOZ_SPACE'
}

NEWSLETTER_NAMES = NEWSLETTERS.keys()
NEWSLETTER_FIELDS = NEWSLETTERS.values()

# Utility functions

def newsletter_field(name):
    return NEWSLETTERS.get(name, False)


def newsletter_name(field):
    i = NEWSLETTER_FIELDS.index(field)
    return NEWSLETTER_NAMES[i]


def logged_in(f):
    """ Decorator to check if the user has permission to view these
    pages """

    @wraps(f)
    def wrapper(request, token, *args, **kwargs):
        subscriber = Subscriber.objects.filter(token=token)
        if not subscriber.exists():
            return json_response({'desc': 'Must have valid token for this request'},
                                 status=403)
        
        request.subscriber = subscriber[0]
        return f(request, token, *args, **kwargs)
    return wrapper


def json_response(data, status=200):
    res = HttpResponse(json.dumps(data),
                       mimetype='application/json')
    res.status_code = status

    # Allow all cross-domain requests, this service will restrict
    # access on the server level
    res['Access-Control-Allow-Origin'] = '*'
    return res


class Update(object):
    SUBSCRIBE=1
    UNSUBSCRIBE=2
    SET=3


@csrf_exempt
def subscribe(request):
    if 'newsletters' not in request.POST:
        return json_response({'desc': 'newsletters is missing'},
                             status=500)

    return update_user(request, Update.SUBSCRIBE)


@logged_in
@csrf_exempt
def unsubscribe(request, token):
    data = request.POST.copy()

    if data.get('optout', 'N') == 'Y':
        data['optin'] = 'N'

        for field in NEWSLETTER_FIELDS:
            data['newsletters'] = ','.join(NEWSLETTER_NAMES)

    return update_user(request, Update.UNSUBSCRIBE, data)


@logged_in
@csrf_exempt
def user(request, token):
    if request.method == 'POST':
        return update_user(request, Update.SET)

    newsletters = NEWSLETTERS.values()

    fields = [
        'EMAIL_ADDRESS_',
        'EMAIL_FORMAT_',
        'COUNTRY_',
        'LANGUAGE_ISO2'
    ]

    for nl in newsletters:
        fields.append('%s_FLG' % nl)

    try:
        rs = Responsys()
        rs.login(settings.RESPONSYS_USER, settings.RESPONSYS_PASS)
        user = rs.retrieve_list_members(request.subscriber.email,
                                        settings.RESPONSYS_FOLDER,
                                        settings.RESPONSYS_LIST,
                                        fields)
    except NewsletterException, e:
        return json_response({'desc': e.message},
                             status=500)
    except UnauthorizedException, e:
        return json_response({'desc': 'Responsys auth failure'},
                             status=500)

    user_data = {
        'email': request.subscriber.email,
        'format': user['EMAIL_FORMAT_'],
        'country': user['COUNTRY_'],
        'lang': user['LANGUAGE_ISO2'],
        'newsletters': [newsletter_name(nl) for nl in newsletters
                        if user.get('%s_FLG' % nl, False) == 'Y']
    }

    rs.logout()

    return json_response(user_data)


def parse_newsletters(record, type, newsletters, optout):
    """ Parse the newsletter data from a comma-delimited string and
    set the appropriate fields in the record """

    newsletters = [x.strip() for x in newsletters.split(',')]

    if type == Update.SUBSCRIBE or type == Update.SET:
        # Subscribe the user to these newsletters
        for nl in newsletters:
            name = newsletter_field(nl)
            if name:
                record['%s_FLG' % name] = 'N' if optout else 'Y'
                record['%s_DATE' % name] = date.today().strftime('%Y-%m-%d')

    
    if type == Update.UNSUBSCRIBE or type == Update.SET:
        # Unsubscribe the user to these newsletters
        unsubs = newsletters

        if type == Update.SET:
            # Unsubscribe to the inversion of these newsletters
            subs = set(newsletters)
            all = set(NEWSLETTER_NAMES)
            unsubs = all.difference(subs)

        for nl in unsubs:
            name = newsletter_field(nl)
            if name:
                record['%s_FLG' % name] = 'N'


def update_user(request, type, data=None):
    """ General method for updating user's preferences and subscribed
    newsletters. Assumes data to be in POST """

    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST supported")

    data = data or request.POST
    has_auth = hasattr(request, 'subscriber')
    
    # validate parameters
    if not has_auth and 'email' not in data:
        return json_response({'desc': 'email is required when not using tokens'},
                             status=500)

    # parse the parameters
    record = {'EMAIL_ADDRESS_': data['email'],
              'EMAIL_PERMISSION_STATUS_': 'I'}
    
    extra_fields = {
        'format': 'EMAIL_FORMAT_',
        'country': 'COUNTRY_',
        'lang': 'LANGUAGE_ISO2',
        'locale': 'LANG_LOCALE',
        'source_url': 'SOURCE_URL'
    }

    # optionally add more fields
    for field in extra_fields.keys():
        if field in data:
            record[extra_fields[field]] = data[field]

    # setup the newsletter fields
    parse_newsletters(record,
                      type,
                      data.get('newsletters', ''),
                      data.get('optin', 'Y') != 'Y')

    # make a new token
    token = str(uuid.uuid4())

    if type == Update.SUBSCRIBE:
        # if we are subscribing and the user already exists, don't
        # update the token. otherwise create a new user with the token.
        try:
            sub = Subscriber.objects.get(email=record['EMAIL_ADDRESS_'])
            token = sub.token
        except Subscriber.DoesNotExist:
            sub = Subscriber(email=record['EMAIL_ADDRESS_'], token=token)
            sub.save()
    else:
        # if we are updating an existing user, set a new token
        sub = Subscriber.objects.get(email=request.subscriber.email)
        sub.token = token
        sub.save()

    record['TOKEN'] = token

    # save the user's fields
    try:
        rs = Responsys()
        rs.login(settings.RESPONSYS_USER, settings.RESPONSYS_PASS)

        if has_auth and record['EMAIL_ADDRESS_'] != request.subscriber.email:
            # email has changed, we need to delete the previous user
            rs.delete_list_members(request.subscriber.email,
                                   settings.RESPONSYS_FOLDER,
                                   settings.RESPONSYS_LIST)

        rs.merge_list_members(settings.RESPONSYS_FOLDER,
                              settings.RESPONSYS_LIST,
                              record.keys(),
                              record.values())
        
        if data.get('trigger_welcome', False) == 'Y':
            rs.trigger_custom_event(record['EMAIL_ADDRESS_'],
                                    settings.RESPONSYS_FOLDER,
                                    settings.RESPONSYS_LIST,
                                    'New_Signup_Welcome')

        rs.logout()
    except NewsletterException, e:
        return json_response({'desc': e.message},
                             status=500)
    except UnauthorizedException, e:
        return json_response({'desc': 'Responsys auth failure'},
                             status=500)
        

    return json_response({'token': token})


@logged_in
@csrf_exempt
def delete_user(request, token):
    try:
        rs = Responsys()
        rs.login(settings.RESPONSYS_USER, settings.RESPONSYS_PASS)
        rs.delete_list_members(request.subscriber.email,
                               settings.RESPONSYS_FOLDER,
                               settings.RESPONSYS_LIST)
        rs.logout()
    except NewsletterException, e:
        return json_response({'desc': e.message},
                             status=500)
    except UnauthorizedException, e:
        return json_response({'desc': 'Responsys auth failure'},
                             status=500)

    request.subscriber.delete()
    return json_response({})
<<<<<<< HEAD
=======

def debug_user(request):
    if not 'email' in request.GET or not 'supertoken' in request.GET:
        return json_response(
            {'status': 'error',
             'desc': 'Using debug_user, you need to pass the '
                     '`email` and `supertoken` GET parameters'},
            status=500
        )

    if request.GET['supertoken'] != settings.SUPERTOKEN:
        return json_response({'status': 'error',
                              'desc': 'Bad supertoken'},
                             status=401)

    return get_user(request.GET['email'])


# Custom update methods

@csrf_exempt
def custom_unsub_reason(request):
    """Update the reason field for the user, which logs why the user
    unsubscribed from all newsletters."""

    if not 'email' in request.POST or not 'reason' in request.POST:
        return json_response(
            {'status': 'error',
             'desc': 'custom_unsub_reason requires the `email` '
                     'and `reason` POST parameters'},
            status=401
        )

    email = request.POST['email']
    reason = request.POST['reason']

    rs = Responsys()
    rs.login(settings.RESPONSYS_USER, settings.RESPONSYS_PASS)
    rs.merge_list_members(settings.RESPONSYS_FOLDER,
                          settings.RESPONSYS_LIST,
                          ['EMAIL_ADDRESS_', 'UNSUBSCRIBE_REASON'],
                          [email, reason])
    rs.logout()

    return json_response({'status': 'ok'})
>>>>>>> 63deaf4... add custom method unsub_reason
