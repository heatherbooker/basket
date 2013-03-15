from django.conf.urls.defaults import patterns, url
from views import (subscribe, subscribe_sms, unsubscribe, user, confirm,
                   debug_user, custom_unsub_reason, custom_student_reps,
                   custom_update_phonebook, newsletters)


urlpatterns = patterns('',
    url('^subscribe/$', subscribe),
    url('^subscribe_sms/$', subscribe_sms),
    url('^unsubscribe/(.*)/$', unsubscribe),
    url('^user/(.*)/$', user),
    url('^confirm/(.*)/$', confirm),
    url('^debug-user/$', debug_user),

    url('^custom_unsub_reason/$', custom_unsub_reason),
    url('^custom_student_reps/$', custom_student_reps),
    url('^custom_update_phonebook/(.*)/$', custom_update_phonebook),

    url('^newsletters/$', newsletters, name='newsletters_api'),
)
