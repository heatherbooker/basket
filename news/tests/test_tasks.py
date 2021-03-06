from copy import deepcopy
from urllib2 import URLError

from django.core.cache import cache
from django.test import TestCase
from django.test.utils import override_settings

from mock import ANY, Mock, patch

from news.celery import app as celery_app
from news.models import FailedTask
from news.newsletters import clear_sms_cache
from news.tasks import (
    add_fxa_activity,
    add_sms_user,
    et_task,
    mogrify_message_id,
    NewsletterException,
    process_donation,
    RECOVERY_MESSAGE_ID,
    send_recovery_message_task,
    send_message,
    get_lock,
    RetryTask,
)


@override_settings(TASK_LOCKING_ENABLE=False)
@patch('news.tasks.get_user_data')
@patch('news.tasks.sfdc')
class ProcessDonationTests(TestCase):
    donate_data = {
        'timestamp': u'2016-11-21T16:46:49.327Z',
        'data': {
            'created': 1479746809327,
            'currency': u'USD',
            'donation_amount': u'75.00',
            'email': u'dude@example.com',
            'first_name': u'Jeffery',
            'last_name': u'Lebowski',
            'project': u'mozillafoundation',
            'source_url': 'https://example.com/donate',
            'recurring': True,
            'service': u'paypal',
            'transaction_id': u'NLEKFRBED3BQ614797468093.25',
        },
    }

    def test_one_name(self, sfdc_mock, gud_mock):
        data = deepcopy(self.donate_data)
        gud_mock.return_value = {
            'id': '1234',
            'first_name': '',
            'last_name': '_',
        }
        del data['data']['first_name']
        data['data']['last_name'] = 'Donnie'
        process_donation(data)
        sfdc_mock.update.assert_called_with(gud_mock(), {
            '_set_subscriber': False,
            'last_name': 'Donnie',
        })

    def test_name_splitting(self, sfdc_mock, gud_mock):
        data = deepcopy(self.donate_data)
        gud_mock.return_value = None
        del data['data']['first_name']
        data['data']['last_name'] = 'Theodore Donald Kerabatsos'
        with self.assertRaises(RetryTask):
            # raises retry b/c the 2nd call to get_user_data returns None
            process_donation(data)
        sfdc_mock.add.assert_called_with({
            '_set_subscriber': False,
            'token': ANY,
            'record_type': ANY,
            'email': 'dude@example.com',
            'first_name': 'Theodore Donald',
            'last_name': 'Kerabatsos',
        })

    def test_only_update_contact_if_modified(self, sfdc_mock, gud_mock):
        data = deepcopy(self.donate_data)
        gud_mock.return_value = {
            'id': '1234',
            'first_name': '',
            'last_name': '_',
        }
        process_donation(data)
        sfdc_mock.update.assert_called_with(gud_mock(), {
            '_set_subscriber': False,
            'first_name': 'Jeffery',
            'last_name': 'Lebowski',
        })

        sfdc_mock.reset_mock()
        data = deepcopy(self.donate_data)
        gud_mock.return_value = {
            'id': '1234',
            'first_name': 'Jeffery',
            'last_name': 'Lebowski',
        }
        process_donation(data)
        sfdc_mock.update.assert_not_called()

    def test_donation_data(self, sfdc_mock, gud_mock):
        data = deepcopy(self.donate_data)
        gud_mock.return_value = {
            'id': '1234',
            'first_name': 'Jeffery',
            'last_name': 'Lebowski',
        }
        process_donation(data)
        sfdc_mock.opportunity.create.assert_called_with({
            'RecordTypeId': ANY,
            'Name': 'Foundation Donation',
            'Donation_Contact__c': '1234',
            'StageName': 'Closed Won',
            'CloseDate': data['timestamp'],
            'Amount': float(data['data']['donation_amount']),
            'Currency__c': 'USD',
            'Payment_Source__c': 'paypal',
            'PMT_Transaction_ID__c': data['data']['transaction_id'],
            'Payment_Type__c': 'Recurring',
            'SourceURL__c': data['data']['source_url'],
            'Project__c': data['data']['project'],
        })


@override_settings(TASK_LOCKING_ENABLE=True)
class TaskDuplicationLockingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_locks_work(self):
        """Calling get_lock more than once quickly with the same key should be locked"""
        get_lock('dude@example.com')
        with self.assertRaises(RetryTask):
            get_lock('dude@example.com')

    def test_lock_prefix_works(self):
        """Should allow same key to not lock other prefixes"""
        get_lock('dude@example.com', prefix='malibu')
        get_lock('dude@example.com', prefix='in-n-out')
        with self.assertRaises(RetryTask):
            get_lock('dude@example.com', prefix='malibu')

    @patch('news.tasks.cache')
    def test_locks_do_not_leak_info(self, cache_mock):
        """Should not use plaintext key in lock name"""
        email = 'donny@example.com'
        cache_mock.add.return_value = True
        get_lock(email)
        key = cache_mock.add.call_args[0][0]
        self.assertNotIn(email, key)


class FailedTaskTest(TestCase):
    """Test that failed tasks are logged in our FailedTask table"""

    @patch('news.tasks.sfmc')
    def test_failed_task_logging(self, mock_sfmc):
        """Failed task is logged in FailedTask table"""
        mock_sfmc.send_mail.side_effect = Exception("Test exception")
        self.assertEqual(0, FailedTask.objects.count())
        args = ['msg_id', 'you@example.com', 'SFDCID']
        kwargs = {'token': 3}
        result = send_message.apply(args=args, kwargs=kwargs)
        fail = FailedTask.objects.get()
        self.assertEqual('news.tasks.send_message', fail.name)
        self.assertEqual(result.task_id, fail.task_id)
        self.assertEqual(args, fail.args)
        self.assertEqual(kwargs, fail.kwargs)
        self.assertEqual(u"Exception('Test exception',)", fail.exc)
        self.assertIn("Exception: Test exception", fail.einfo)


class RetryTaskTest(TestCase):
    """Test that we can retry a task"""
    @patch('django.contrib.messages.info', autospec=True)
    def test_retry_task(self, info):
        TASK_NAME = 'news.tasks.update_phonebook'
        failed_task = FailedTask(name=TASK_NAME,
                                 task_id=4,
                                 args=[1, 2],
                                 kwargs={'token': 3},
                                 exc='',
                                 einfo='')
        # Failed task is deleted after queuing, but that only works on records
        # that have been saved, so just mock that and check later that it was
        # called.
        failed_task.delete = Mock(spec=failed_task.delete)
        with patch.object(celery_app, 'send_task') as send_task_mock:
            # Let's retry.
            failed_task.retry()
        # Task was submitted again
        send_task_mock.assert_called_with(TASK_NAME, args=[1, 2], kwargs={'token': 3})
        # Previous failed task was deleted
        self.assertTrue(failed_task.delete.called)


@patch('news.tasks.send_message', autospec=True)
@patch('news.tasks.get_user_data', autospec=True)
class RecoveryMessageTask(TestCase):
    def setUp(self):
        self.email = "dude@example.com"

    def test_unknown_email(self, mock_look_for_user, mock_send):
        """Email not in basket or ET"""
        # Should log error and return
        mock_look_for_user.return_value = None
        send_recovery_message_task(self.email)
        self.assertFalse(mock_send.called)

    def test_et_error(self, mock_look_for_user, mock_send):
        """Error talking to Basket. I'm shocked, SHOCKED!"""
        mock_look_for_user.side_effect = NewsletterException('ET has failed to achieve.')

        with self.assertRaises(NewsletterException):
            send_recovery_message_task(self.email)

        self.assertFalse(mock_send.called)

    @override_settings(RECOVER_MSG_LANGS=['fr'])
    def test_email_in_et(self, mock_look_for_user, mock_send):
        """Email not in basket but in ET"""
        # Should trigger message. We can follow the user's format and lang pref
        format = 'T'
        lang = 'fr'
        mock_look_for_user.return_value = {
            'id': 'SFDCID',
            'status': 'ok',
            'email': self.email,
            'format': format,
            'country': '',
            'lang': lang,
            'token': 'USERTOKEN',
            'newsletters': [],
        }
        send_recovery_message_task(self.email)
        message_id = mogrify_message_id(RECOVERY_MESSAGE_ID, lang, format)
        mock_send.delay.assert_called_with(message_id, self.email, 'SFDCID',
                                           token='USERTOKEN')

    @override_settings(RECOVER_MSG_LANGS=['en'])
    def test_lang_not_available(self, mock_look_for_user, mock_send):
        """Language not available for recover message"""
        # Should trigger message in english if not available in user lang
        format = 'T'
        mock_look_for_user.return_value = {
            'id': 'SFDCID',
            'status': 'ok',
            'email': self.email,
            'format': format,
            'country': '',
            'lang': 'fr',
            'token': 'USERTOKEN',
            'newsletters': [],
        }
        send_recovery_message_task(self.email)
        message_id = mogrify_message_id(RECOVERY_MESSAGE_ID, 'en', format)
        mock_send.delay.assert_called_with(message_id, self.email, 'SFDCID',
                                           token='USERTOKEN')


@override_settings(ET_CLIENT_ID='client_id', ET_CLIENT_SECRET='client_secret')
@patch('news.newsletters.SMS_MESSAGES', {'foo': 'bar'})
class AddSMSUserTests(TestCase):
    def setUp(self):
        clear_sms_cache()
        patcher = patch('news.backends.sfmc.sfmc.send_sms')
        self.send_sms = patcher.start()
        self.addCleanup(patcher.stop)

    def test_send_name_invalid(self):
        """If the send_name is invalid, return immediately."""
        add_sms_user('baffle', '8675309', False)
        self.assertFalse(self.send_sms.called)

    def test_success(self):
        add_sms_user('foo', '8675309', False)
        self.send_sms.assert_called_with(['8675309'], 'bar')

    def test_success_with_optin(self):
        """
        If optin is True, add a Mobile_Subscribers record for the
        number.
        """
        with patch('news.tasks.sfmc') as sfmc_mock:
            add_sms_user('foo', '8675309', True)

            sfmc_mock.add_row.assert_called_with('Mobile_Subscribers', {
                'Phone': '8675309',
                'SubscriberKey': '8675309',
            })


class ETTaskTests(TestCase):
    def test_retry_increase(self):
        """
        The delay for retrying a task should increase geometrically by a
        power of 2. I really hope I said that correctly.
        """
        error = URLError(reason=Exception('foo bar!'))

        @et_task
        def myfunc():
            raise error

        myfunc.push_request(retries=4)
        myfunc.retry = Mock(side_effect=Exception)
        # have to use run() to make sure our request above is used
        with self.assertRaises(Exception):
            myfunc.run()

        myfunc.retry.assert_called_with(countdown=32 * 60)


class AddFxaActivityTests(TestCase):
    def _base_test(self, user_agent=False, fxa_id='123', first_device=True):
        if not user_agent:
            user_agent = 'Mozilla/5.0 (Windows NT 6.1; rv:10.0) Gecko/20100101 Firefox/10.0'

        data = {
            'fxa_id': fxa_id,
            'first_device': first_device,
            'user_agent': user_agent,
        }

        with patch('news.tasks.apply_updates') as apply_updates_mock:
            add_fxa_activity(data)
        record = apply_updates_mock.call_args[0][1]
        return record

    def test_login_date(self):
        with patch('news.tasks.gmttime') as gmttime_mock:
            gmttime_mock.return_value = 'this is time'
            record = self._base_test()
        self.assertEqual(record['LOGIN_DATE'], 'this is time')

    def test_first_device(self):
        record = self._base_test(first_device=True)
        self.assertEqual(record['FIRST_DEVICE'], 'y')

        record = self._base_test(first_device=False)
        self.assertEqual(record['FIRST_DEVICE'], 'n')

    def test_fxa_id(self):
        record = self._base_test(fxa_id='This is id')
        self.assertEqual(record['FXA_ID'], 'This is id')

    def test_windows(self):
        ua = 'Mozilla/5.0 (Windows NT 6.1; rv:10.0) Gecko/20100101 Firefox/10.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Windows 7')
        self.assertEqual(record['OS_VERSION'], '')  # Not sure if we expect '7' here.
        self.assertEqual(record['BROWSER'], 'Firefox 10')
        self.assertEqual(record['DEVICE_NAME'], 'Other')
        self.assertEqual(record['DEVICE_TYPE'], 'D')

    def test_mac(self):
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.6; rv:10.0) Gecko/20100101 Firefox/30.2'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Mac OS X')
        self.assertEqual(record['OS_VERSION'], '10.6')
        self.assertEqual(record['BROWSER'], 'Firefox 30.2')
        self.assertEqual(record['DEVICE_NAME'], 'Other')
        self.assertEqual(record['DEVICE_TYPE'], 'D')

    def test_linux(self):
        ua = 'Mozilla/5.0 (X11; Linux i686 on x86_64; rv:10.0) Gecko/20100101 Firefox/42.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Linux')
        self.assertEqual(record['OS_VERSION'], '')
        self.assertEqual(record['BROWSER'], 'Firefox 42')
        self.assertEqual(record['DEVICE_NAME'], 'Other')
        self.assertEqual(record['DEVICE_TYPE'], 'D')

    def test_android_phone_below_version_41(self):
        ua = 'Mozilla/5.0 (Android; Mobile; rv:40.0) Gecko/40.0 Firefox/40.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Android')
        self.assertEqual(record['OS_VERSION'], '')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 40')
        self.assertEqual(record['DEVICE_NAME'], 'Generic Smartphone')
        self.assertEqual(record['DEVICE_TYPE'], 'M')

    def test_android_tablet_below_version_41(self):
        ua = 'Mozilla/5.0 (Android; Tablet; rv:40.0) Gecko/40.0 Firefox/40.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Android')
        self.assertEqual(record['OS_VERSION'], '')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 40')
        self.assertEqual(record['DEVICE_NAME'], 'Generic Tablet')
        self.assertEqual(record['DEVICE_TYPE'], 'T')

    def test_android_phone_from_version_41(self):
        ua = 'Mozilla/5.0 (Android 4.4; Mobile; rv:41.0) Gecko/41.0 Firefox/41.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Android')
        self.assertEqual(record['OS_VERSION'], '4.4')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 41')
        self.assertEqual(record['DEVICE_NAME'], 'Generic Smartphone')
        self.assertEqual(record['DEVICE_TYPE'], 'M')

    # TODO This reports Android 5 instead of Firefox 40
    #
    # def test_android_tablet_from_version_41(self):
    #     ua = 'Mozilla/5.0 (Android 5.0; Tablet; rv:41.0) Gecko/41.0 Firefox/41.0'
    #     record = self._base_test(ua)
    #     self.assertEqual(record['OS'], 'Android')
    #     self.assertEqual(record['OS_VERSION'], '5')
    #     self.assertEqual(record['BROWSER'], 'Firefox 40')
    #     self.assertEqual(record['DEVICE_NAME'], 'Generic Tablet')
    #     self.assertEqual(record['DEVICE_TYPE'], 'T')

    def test_firefox_os_phone(self):
        ua = 'Mozilla/5.0 (Mobile; rv:26.0) Gecko/26.0 Firefox/26.0'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Firefox OS')
        self.assertEqual(record['OS_VERSION'], '1.2')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 26')
        self.assertEqual(record['DEVICE_NAME'], 'Generic Smartphone')
        self.assertEqual(record['DEVICE_TYPE'], 'M')

    def test_firefox_os_tablet(self):
        ua = 'Mozilla/5.0 (Tablet; rv:26.0) Gecko/26.0 Firefox/26.0'
        record = self._base_test(ua)

        self.assertEqual(record['OS'], 'Firefox OS')
        self.assertEqual(record['OS_VERSION'], '1.2')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 26')
        self.assertEqual(record['DEVICE_NAME'], 'Generic Tablet')
        self.assertEqual(record['DEVICE_TYPE'], 'T')

    def test_firefox_os_device_specific(self):
        ua = 'Mozilla/5.0 (Mobile; ZTEOPEN; rv:18.1) Gecko/18.1 Firefox/18.1'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'Firefox OS')
        self.assertEqual(record['OS_VERSION'], '1.1')
        self.assertEqual(record['BROWSER'], 'Firefox Mobile 18.1')
        self.assertEqual(record['DEVICE_NAME'], 'ZTE OPEN')
        self.assertEqual(record['DEVICE_TYPE'], 'M')

    def test_firefox_ios_iphone(self):
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 8_3 like Mac OS X) AppleWebKit/600.1.4 (KHTML, like Gecko) FxiOS/1.0 Mobile/12F69 Safari/600.1.4'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'iOS')
        self.assertEqual(record['OS_VERSION'], '8.3')
        self.assertEqual(record['BROWSER'], 'Firefox iOS 1')
        self.assertEqual(record['DEVICE_NAME'], 'iPhone')
        self.assertEqual(record['DEVICE_TYPE'], 'M')

    def test_firefox_ios_tablet(self):
        ua = 'Mozilla/5.0 (iPad; CPU iPhone OS 8_3 like Mac OS X) AppleWebKit/600.1.4 (KHTML, like Gecko) FxiOS/1.0 Mobile/12F69 Safari/600.1.4'
        record = self._base_test(ua)
        self.assertEqual(record['OS'], 'iOS')
        self.assertEqual(record['OS_VERSION'], '8.3')
        self.assertEqual(record['BROWSER'], 'Firefox iOS 1')
        self.assertEqual(record['DEVICE_NAME'], 'iPad')
        self.assertEqual(record['DEVICE_TYPE'], 'T')
