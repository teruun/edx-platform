# coding:utf-8
"""
Tests for student activation and login
"""
from __future__ import absolute_import

import datetime
import json
import unicodedata

import ddt
import six
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.test import TestCase
from django.test.client import Client
from django.test.utils import override_settings
from django.urls import NoReverseMatch, reverse
from mock import patch
from six.moves import range

from openedx.core.djangoapps.password_policy.compliance import (
    NonCompliantPasswordException,
    NonCompliantPasswordWarning
)
from openedx.core.djangoapps.user_api.config.waffle import PREVENT_AUTH_USER_WRITES, waffle
from openedx.core.djangoapps.user_api.accounts import EMAIL_MIN_LENGTH, EMAIL_MAX_LENGTH
from openedx.core.djangoapps.user_authn.cookies import jwt_cookies
from openedx.core.djangoapps.user_authn.views.login import (
    shim_student_view,
    AllowedAuthUser,
    UPDATE_LOGIN_USER_ERROR_STATUS_CODE,
    ENABLE_LOGIN_USING_THIRDPARTY_AUTH_ONLY
)
from openedx.core.djangoapps.user_authn.tests.utils import setup_login_oauth_client
from openedx.core.djangolib.testing.utils import CacheIsolationTestCase, skip_unless_lms
from openedx.core.djangoapps.site_configuration.tests.mixins import SiteMixin
from openedx.core.lib.api.test_utils import ApiTestCase
from student.tests.factories import RegistrationFactory, UserFactory, UserProfileFactory
from util.password_policy_validators import DEFAULT_MAX_PASSWORD_LENGTH


@ddt.ddt
class LoginTest(SiteMixin, CacheIsolationTestCase):
    """
    Test login_user() view
    """

    ENABLED_CACHES = ['default']
    LOGIN_FAILED_WARNING = 'Email or password is incorrect'
    ACTIVATE_ACCOUNT_WARNING = 'In order to sign in, you need to activate your account'
    username = 'test'
    user_email = 'test@edx.org'
    password = 'test_password'

    def setUp(self):
        """Setup a test user along with its registration and profile"""
        super(LoginTest, self).setUp()
        self.user = self._create_user(self.username, self.user_email)

        RegistrationFactory(user=self.user)
        UserProfileFactory(user=self.user)

        self.client = Client()
        cache.clear()

        self.url = reverse('login_api')

    def _create_user(self, username, user_email):
        user = UserFactory.build(username=username, email=user_email)
        user.set_password(self.password)
        user.save()
        return user

    def test_login_success(self):
        response, mock_audit_log = self._login_response(
            self.user_email, self.password, patched_audit_log='student.models.AUDIT_LOG'
        )
        self._assert_response(response, success=True)
        self._assert_audit_log(mock_audit_log, 'info', [u'Login success', self.user_email])

    @patch.dict("django.conf.settings.FEATURES", {'SQUELCH_PII_IN_LOGS': True})
    @ddt.data(True, False)
    def test_login_success_no_pii(self, is_error_status_code_enabled):
        with UPDATE_LOGIN_USER_ERROR_STATUS_CODE.override(is_error_status_code_enabled):
            response, mock_audit_log = self._login_response(
                self.user_email, self.password, patched_audit_log='student.models.AUDIT_LOG'
            )
        self._assert_response(response, success=True)
        self._assert_audit_log(mock_audit_log, 'info', [u'Login success'])
        self._assert_not_in_audit_log(mock_audit_log, 'info', [self.user_email])

    def test_login_success_unicode_email(self):
        unicode_email = u'test' + six.unichr(40960) + u'@edx.org'
        self.user.email = unicode_email
        self.user.save()

        response, mock_audit_log = self._login_response(
            unicode_email, self.password, patched_audit_log='student.models.AUDIT_LOG'
        )
        self._assert_response(response, success=True)
        self._assert_audit_log(mock_audit_log, 'info', [u'Login success', unicode_email])

    def test_last_login_updated(self):
        old_last_login = self.user.last_login
        self.test_login_success()
        self.user.refresh_from_db()
        assert self.user.last_login > old_last_login

    def test_login_success_prevent_auth_user_writes(self):
        with waffle().override(PREVENT_AUTH_USER_WRITES, True):
            old_last_login = self.user.last_login
            self.test_login_success()
            self.user.refresh_from_db()
            assert old_last_login == self.user.last_login

    @ddt.data(
        (True, 400),
        (False, 200),
    )
    @ddt.unpack
    def test_login_fail_no_user_exists(self, is_error_status_code_enabled, expected_status_code):
        nonexistent_email = u'not_a_user@edx.org'
        with UPDATE_LOGIN_USER_ERROR_STATUS_CODE.override(is_error_status_code_enabled):
            response, mock_audit_log = self._login_response(
                nonexistent_email,
                self.password,
            )
        self._assert_response(
            response, success=False, value=self.LOGIN_FAILED_WARNING, status_code=expected_status_code
        )
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'Unknown user email', nonexistent_email])

    @patch.dict("django.conf.settings.FEATURES", {'SQUELCH_PII_IN_LOGS': True})
    def test_login_fail_no_user_exists_no_pii(self):
        nonexistent_email = u'not_a_user@edx.org'
        response, mock_audit_log = self._login_response(
            nonexistent_email,
            self.password,
        )
        self._assert_response(response, success=False, value=self.LOGIN_FAILED_WARNING)
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'Unknown user email'])
        self._assert_not_in_audit_log(mock_audit_log, 'warning', [nonexistent_email])

    def test_login_fail_wrong_password(self):
        response, mock_audit_log = self._login_response(
            self.user_email,
            'wrong_password',
        )
        self._assert_response(response, success=False, value=self.LOGIN_FAILED_WARNING)
        self._assert_audit_log(mock_audit_log, 'warning',
                               [u'Login failed', u'password for', self.user_email, u'invalid'])

    @patch.dict("django.conf.settings.FEATURES", {'SQUELCH_PII_IN_LOGS': True})
    def test_login_fail_wrong_password_no_pii(self):
        response, mock_audit_log = self._login_response(self.user_email, 'wrong_password')
        self._assert_response(response, success=False, value=self.LOGIN_FAILED_WARNING)
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'password for', u'invalid'])
        self._assert_not_in_audit_log(mock_audit_log, 'warning', [self.user_email])

    @patch.dict("django.conf.settings.FEATURES", {'SQUELCH_PII_IN_LOGS': True})
    def test_login_not_activated_no_pii(self):
        # De-activate the user
        self.user.is_active = False
        self.user.save()

        # Should now be unable to login
        response, mock_audit_log = self._login_response(
            self.user_email,
            self.password
        )
        self._assert_response(response, success=False,
                              value="In order to sign in, you need to activate your account.")
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'Account not active for user'])
        self._assert_not_in_audit_log(mock_audit_log, 'warning', [u'test'])

    def test_login_not_activated_with_correct_credentials(self):
        """
        Tests that when user login with the correct credentials but with an inactive
        account, the system, send account activation email notification to the user.
        """
        self.user.is_active = False
        self.user.save()

        response, mock_audit_log = self._login_response(
            self.user_email,
            self.password,
        )
        self._assert_response(response, success=False, value=self.ACTIVATE_ACCOUNT_WARNING)
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'Account not active for user'])

    @patch('openedx.core.djangoapps.user_authn.views.login._log_and_raise_inactive_user_auth_error')
    def test_login_inactivated_user_with_incorrect_credentials(self, mock_inactive_user_email_and_error):
        """
        Tests that when user login with incorrect credentials and an inactive account,
        the system does *not* send account activation email notification to the user.
        """
        nonexistent_email = 'incorrect@email.com'
        self.user.is_active = False
        self.user.save()
        response, mock_audit_log = self._login_response(nonexistent_email, 'incorrect_password')

        self.assertFalse(mock_inactive_user_email_and_error.called)
        self._assert_response(response, success=False, value=self.LOGIN_FAILED_WARNING)
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', u'Unknown user email', nonexistent_email])

    def test_login_unicode_email(self):
        unicode_email = self.user_email + six.unichr(40960)
        response, mock_audit_log = self._login_response(
            unicode_email,
            self.password,
        )
        self._assert_response(response, success=False)
        self._assert_audit_log(mock_audit_log, 'warning', [u'Login failed', unicode_email])

    def test_login_unicode_password(self):
        unicode_password = self.password + six.unichr(1972)
        response, mock_audit_log = self._login_response(
            self.user_email,
            unicode_password,
        )
        self._assert_response(response, success=False)
        self._assert_audit_log(mock_audit_log, 'warning',
                               [u'Login failed', u'password for', self.user_email, u'invalid'])

    def test_logout_logging(self):
        response, _ = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)
        logout_url = reverse('logout')
        with patch('student.models.AUDIT_LOG') as mock_audit_log:
            response = self.client.post(logout_url)
        self.assertEqual(response.status_code, 200)
        self._assert_audit_log(mock_audit_log, 'info', [u'Logout', u'test'])

    def test_login_user_info_cookie(self):
        response, _ = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)

        # Verify the format of the "user info" cookie set on login
        cookie = self.client.cookies[settings.EDXMKTG_USER_INFO_COOKIE_NAME]
        user_info = json.loads(cookie.value)

        self.assertEqual(user_info["version"], settings.EDXMKTG_USER_INFO_COOKIE_VERSION)
        self.assertEqual(user_info["username"], self.user.username)

        # Check that the URLs are absolute
        for url in user_info["header_urls"].values():
            self.assertIn("http://testserver/", url)

    def test_logout_deletes_mktg_cookies(self):
        response, _ = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)

        # Check that the marketing site cookies have been set
        self.assertIn(settings.EDXMKTG_LOGGED_IN_COOKIE_NAME, self.client.cookies)
        self.assertIn(settings.EDXMKTG_USER_INFO_COOKIE_NAME, self.client.cookies)

        # Log out
        logout_url = reverse('logout')
        response = self.client.post(logout_url)

        # Check that the marketing site cookies have been deleted
        # (cookies are deleted by setting an expiration date in 1970)
        for cookie_name in [settings.EDXMKTG_LOGGED_IN_COOKIE_NAME, settings.EDXMKTG_USER_INFO_COOKIE_NAME]:
            cookie = self.client.cookies[cookie_name]
            self.assertIn("01-Jan-1970", cookie.get('expires'))

    @override_settings(
        EDXMKTG_LOGGED_IN_COOKIE_NAME=u"unicode-logged-in",
        EDXMKTG_USER_INFO_COOKIE_NAME=u"unicode-user-info",
    )
    def test_unicode_mktg_cookie_names(self):
        # When logged in cookie names are loaded from JSON files, they may
        # have type `unicode` instead of `str`, which can cause errors
        # when calling Django cookie manipulation functions.
        response, _ = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)

        response = self.client.post(reverse('logout'))
        expected = {
            'target': '/',
        }
        self.assertDictContainsSubset(expected, response.context_data)

    @patch.dict("django.conf.settings.FEATURES", {'SQUELCH_PII_IN_LOGS': True})
    def test_logout_logging_no_pii(self):
        response, _ = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)
        logout_url = reverse('logout')
        with patch('student.models.AUDIT_LOG') as mock_audit_log:
            response = self.client.post(logout_url)
        self.assertEqual(response.status_code, 200)
        self._assert_audit_log(mock_audit_log, 'info', [u'Logout'])
        self._assert_not_in_audit_log(mock_audit_log, 'info', [u'test'])

    def test_login_ratelimited_success(self):
        # Try (and fail) logging in with fewer attempts than the limit of 30
        # and verify that you can still successfully log in afterwards.
        for i in range(20):
            password = u'test_password{0}'.format(i)
            response, _audit_log = self._login_response(self.user_email, password)
            self._assert_response(response, success=False)
        # now try logging in with a valid password
        response, _audit_log = self._login_response(self.user_email, self.password)
        self._assert_response(response, success=True)

    def test_login_ratelimited(self):
        # try logging in 30 times, the default limit in the number of failed
        # login attempts in one 5 minute period before the rate gets limited
        for i in range(30):
            password = u'test_password{0}'.format(i)
            self._login_response(self.user_email, password)
        # check to see if this response indicates that this was ratelimited
        response, _audit_log = self._login_response(self.user_email, 'wrong_password')
        self._assert_response(response, success=False, value='Too many failed login attempts')

    @patch.dict("django.conf.settings.FEATURES", {"DISABLE_SET_JWT_COOKIES_FOR_TESTS": False})
    def test_login_refresh(self):
        def _assert_jwt_cookie_present(response):
            self.assertEqual(response.status_code, 200)
            self.assertIn(jwt_cookies.jwt_cookie_header_payload_name(), self.client.cookies)

        setup_login_oauth_client()
        response, _ = self._login_response(self.user_email, self.password)
        _assert_jwt_cookie_present(response)

        response = self.client.post(reverse('login_refresh'))
        _assert_jwt_cookie_present(response)

    @patch.dict("django.conf.settings.FEATURES", {"DISABLE_SET_JWT_COOKIES_FOR_TESTS": False})
    def test_login_refresh_anonymous_user(self):
        response = self.client.post(reverse('login_refresh'))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(jwt_cookies.jwt_cookie_header_payload_name(), self.client.cookies)

    @patch.dict("django.conf.settings.FEATURES", {'PREVENT_CONCURRENT_LOGINS': True})
    def test_single_session(self):
        creds = {'email': self.user_email, 'password': self.password}
        client1 = Client()
        client2 = Client()

        response = client1.post(self.url, creds)
        self._assert_response(response, success=True)

        # Reload the user from the database
        self.user = User.objects.get(pk=self.user.pk)

        self.assertEqual(self.user.profile.get_meta()['session_id'], client1.session.session_key)

        # second login should log out the first
        response = client2.post(self.url, creds)
        self._assert_response(response, success=True)

        try:
            # this test can be run with either lms or studio settings
            # since studio does not have a dashboard url, we should
            # look for another url that is login_required, in that case
            url = reverse('dashboard')
        except NoReverseMatch:
            url = reverse('upload_transcripts')
        response = client1.get(url)
        # client1 will be logged out
        self.assertEqual(response.status_code, 302)

    @patch.dict("django.conf.settings.FEATURES", {'PREVENT_CONCURRENT_LOGINS': True})
    def test_single_session_with_no_user_profile(self):
        """
        Assert that user login with cas (Central Authentication Service) is
        redirect to dashboard in case of lms or upload_transcripts in case of
        cms
        """
        user = UserFactory.build(username='tester', email='tester@edx.org')
        user.set_password(self.password)
        user.save()

        # Assert that no profile is created.
        self.assertFalse(hasattr(user, 'profile'))

        creds = {'email': 'tester@edx.org', 'password': self.password}
        client1 = Client()
        client2 = Client()

        response = client1.post(self.url, creds)
        self._assert_response(response, success=True)

        # Reload the user from the database
        user = User.objects.get(pk=user.pk)

        # Assert that profile is created.
        self.assertTrue(hasattr(user, 'profile'))

        # second login should log out the first
        response = client2.post(self.url, creds)
        self._assert_response(response, success=True)

        try:
            # this test can be run with either lms or studio settings
            # since studio does not have a dashboard url, we should
            # look for another url that is login_required, in that case
            url = reverse('dashboard')
        except NoReverseMatch:
            url = reverse('upload_transcripts')
        response = client1.get(url)
        # client1 will be logged out
        self.assertEqual(response.status_code, 302)

    @patch.dict("django.conf.settings.FEATURES", {'PREVENT_CONCURRENT_LOGINS': True})
    def test_single_session_with_url_not_having_login_required_decorator(self):
        # accessing logout url as it does not have login-required decorator it will avoid redirect
        # and go inside the enforce_single_login

        creds = {'email': self.user_email, 'password': self.password}
        client1 = Client()
        client2 = Client()

        response = client1.post(self.url, creds)
        self._assert_response(response, success=True)

        # Reload the user from the database
        self.user = User.objects.get(pk=self.user.pk)

        self.assertEqual(self.user.profile.get_meta()['session_id'], client1.session.session_key)

        # second login should log out the first
        response = client2.post(self.url, creds)
        self._assert_response(response, success=True)

        url = reverse('logout')

        response = client1.get(url)
        self.assertEqual(response.status_code, 200)

    @override_settings(PASSWORD_POLICY_COMPLIANCE_ROLLOUT_CONFIG={'ENFORCE_COMPLIANCE_ON_LOGIN': True})
    def test_check_password_policy_compliance(self):
        """
        Tests _enforce_password_policy_compliance succeeds when no exception is thrown
        """
        enforce_compliance_path = 'openedx.core.djangoapps.password_policy.compliance.enforce_compliance_on_login'
        with patch(enforce_compliance_path) as mock_check_password_policy_compliance:
            mock_check_password_policy_compliance.return_value = HttpResponse()
            response, _ = self._login_response(self.user_email, self.password)
            response_content = json.loads(response.content.decode('utf-8'))
        self.assertTrue(response_content.get('success'))

    @override_settings(PASSWORD_POLICY_COMPLIANCE_ROLLOUT_CONFIG={'ENFORCE_COMPLIANCE_ON_LOGIN': True})
    def test_check_password_policy_compliance_exception(self):
        """
        Tests _enforce_password_policy_compliance fails with an exception thrown
        """
        enforce_compliance_on_login = 'openedx.core.djangoapps.password_policy.compliance.enforce_compliance_on_login'
        with patch(enforce_compliance_on_login) as mock_enforce_compliance_on_login:
            mock_enforce_compliance_on_login.side_effect = NonCompliantPasswordException()
            response, _ = self._login_response(
                self.user_email,
                self.password
            )
            response_content = json.loads(response.content.decode('utf-8'))
        self.assertFalse(response_content.get('success'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Password reset', mail.outbox[0].subject)

    @override_settings(PASSWORD_POLICY_COMPLIANCE_ROLLOUT_CONFIG={'ENFORCE_COMPLIANCE_ON_LOGIN': True})
    def test_check_password_policy_compliance_warning(self):
        """
        Tests _enforce_password_policy_compliance succeeds with a warning thrown
        """
        enforce_compliance_on_login = 'openedx.core.djangoapps.password_policy.compliance.enforce_compliance_on_login'
        with patch(enforce_compliance_on_login) as mock_enforce_compliance_on_login:
            mock_enforce_compliance_on_login.side_effect = NonCompliantPasswordWarning('Test warning')
            response, _ = self._login_response(self.user_email, self.password)
            response_content = json.loads(response.content.decode('utf-8'))
            self.assertIn('Test warning', self.client.session['_messages'])
        self.assertTrue(response_content.get('success'))

    @ddt.data(
        ('test_password', 'test_password', True),
        (unicodedata.normalize('NFKD', u'Ṗŕệṿïệẅ Ṯệẍt'),
         unicodedata.normalize('NFKC', u'Ṗŕệṿïệẅ Ṯệẍt'), False),
        (unicodedata.normalize('NFKC', u'Ṗŕệṿïệẅ Ṯệẍt'),
         unicodedata.normalize('NFKD', u'Ṗŕệṿïệẅ Ṯệẍt'), True),
        (unicodedata.normalize('NFKD', u'Ṗŕệṿïệẅ Ṯệẍt'),
         unicodedata.normalize('NFKD', u'Ṗŕệṿïệẅ Ṯệẍt'), False),
    )
    @ddt.unpack
    def test_password_unicode_normalization_login(self, password, password_entered, login_success):
        """
        Tests unicode normalization on user's passwords on login.
        """
        self.user.set_password(password)
        self.user.save()
        response, _ = self._login_response(self.user.email, password_entered)
        self._assert_response(response, success=login_success)

    def _login_response(self, email, password, patched_audit_log=None, extra_post_params=None):
        """
        Post the login info
        """
        if patched_audit_log is None:
            patched_audit_log = 'openedx.core.djangoapps.user_authn.views.login.AUDIT_LOG'
        post_params = {'email': email, 'password': password}
        if extra_post_params is not None:
            post_params.update(extra_post_params)
        with patch(patched_audit_log) as mock_audit_log:
            result = self.client.post(self.url, post_params)
        return result, mock_audit_log

    def _assert_response(self, response, success=None, value=None, status_code=None):
        """
        Assert that the response has the expected status code and returned a valid
        JSON-parseable dict.

        If success is provided, assert that the response had that
        value for 'success' in the JSON dict.

        If value is provided, assert that the response contained that
        value for 'value' in the JSON dict.
        """
        expected_status_code = status_code or 200
        self.assertEqual(response.status_code, expected_status_code)

        try:
            response_dict = json.loads(response.content.decode('utf-8'))
        except ValueError:
            self.fail(u"Could not parse response content as JSON: %s"
                      % str(response.content))

        if success is not None:
            self.assertEqual(response_dict['success'], success)

        if value is not None:
            msg = (u"'%s' did not contain '%s'" %
                   (six.text_type(response_dict['value']), six.text_type(value)))
            self.assertIn(value, response_dict['value'], msg)

    def _assert_audit_log(self, mock_audit_log, level, log_strings):
        """
        Check that the audit log has received the expected call as its last call.
        """
        method_calls = mock_audit_log.method_calls
        name, args, _kwargs = method_calls[-1]
        self.assertEquals(name, level)
        self.assertEquals(len(args), 1)
        format_string = args[0]
        for log_string in log_strings:
            self.assertIn(log_string, format_string)

    def _assert_not_in_audit_log(self, mock_audit_log, level, log_strings):
        """
        Check that the audit log has received the expected call as its last call.
        """
        method_calls = mock_audit_log.method_calls
        name, args, _kwargs = method_calls[-1]
        self.assertEquals(name, level)
        self.assertEquals(len(args), 1)
        format_string = args[0]
        for log_string in log_strings:
            self.assertNotIn(log_string, format_string)

    @ddt.data(
        {
            'switch_enabled': False,
            'whitelisted': False,
            'allowed_domain': 'edx.org',
            'user_domain': 'edx.org',
            'success': True
        },
        {
            'switch_enabled': False,
            'whitelisted': True,
            'allowed_domain': 'edx.org',
            'user_domain': 'edx.org',
            'success': True
        },
        {
            'switch_enabled': True,
            'whitelisted': False,
            'allowed_domain': 'edx.org',
            'user_domain': 'edx.org',
            'success': False
        },
        {
            'switch_enabled': True,
            'whitelisted': False,
            'allowed_domain': 'fake.org',
            'user_domain': 'edx.org',
            'success': True
        },
        {
            'switch_enabled': True,
            'whitelisted': True,
            'allowed_domain': 'edx.org',
            'user_domain': 'edx.org',
            'success': True
        },
        {
            'switch_enabled': True,
            'whitelisted': False,
            'allowed_domain': 'batman.gotham',
            'user_domain': 'batman.gotham',
            'success': False
        },
    )
    @ddt.unpack
    def test_login_for_user_auth_flow(self, switch_enabled, whitelisted, allowed_domain, user_domain, success):
        """
        Verify that `login._check_user_auth_flow` works as expected.
        """
        username = 'batman'
        user_email = '{username}@{domain}'.format(username=username, domain=user_domain)
        user = self._create_user(username, user_email)

        provider = 'Google'
        site = self.set_up_site(allowed_domain, {
            'SITE_NAME': allowed_domain,
            'THIRD_PARTY_AUTH_ONLY_DOMAIN': allowed_domain,
            'THIRD_PARTY_AUTH_ONLY_PROVIDER': provider
        })

        if whitelisted:
            AllowedAuthUser.objects.create(site=site, email=user.email)
        else:
            AllowedAuthUser.objects.filter(site=site, email=user.email).delete()

        with ENABLE_LOGIN_USING_THIRDPARTY_AUTH_ONLY.override(switch_enabled):
            value = None if success else u'As an {0} user, You must login with your {0} {1} account.'.format(
                allowed_domain,
                provider
            )
            response, __ = self._login_response(user.email, self.password)
            self._assert_response(
                response,
                success=success,
                value=value,
            )


@ddt.ddt
@skip_unless_lms
class LoginSessionViewTest(ApiTestCase):
    """Tests for the login end-points of the user API. """

    USERNAME = "bob"
    EMAIL = "bob@example.com"
    PASSWORD = "password"

    def setUp(self):
        super(LoginSessionViewTest, self).setUp()
        self.url = reverse("user_api_login_session")

    @ddt.data("get", "post")
    def test_auth_disabled(self, method):
        self.assertAuthDisabled(method, self.url)

    def test_allowed_methods(self):
        self.assertAllowedMethods(self.url, ["GET", "POST", "HEAD", "OPTIONS"])

    def test_put_not_allowed(self):
        response = self.client.put(self.url)
        self.assertHttpMethodNotAllowed(response)

    def test_delete_not_allowed(self):
        response = self.client.delete(self.url)
        self.assertHttpMethodNotAllowed(response)

    def test_patch_not_allowed(self):
        response = self.client.patch(self.url)
        self.assertHttpMethodNotAllowed(response)

    def test_login_form(self):
        # Retrieve the login form
        response = self.client.get(self.url, content_type="application/json")
        self.assertHttpOK(response)

        # Verify that the form description matches what we expect
        form_desc = json.loads(response.content.decode('utf-8'))
        self.assertEqual(form_desc["method"], "post")
        self.assertEqual(form_desc["submit_url"], self.url)
        self.assertEqual(form_desc["fields"], [
            {
                "name": "email",
                "defaultValue": "",
                "type": "email",
                "required": True,
                "label": "Email",
                "placeholder": "username@domain.com",
                "instructions": u"The email address you used to register with {platform_name}".format(
                    platform_name=settings.PLATFORM_NAME
                ),
                "restrictions": {
                    "min_length": EMAIL_MIN_LENGTH,
                    "max_length": EMAIL_MAX_LENGTH
                },
                "errorMessages": {},
                "supplementalText": "",
                "supplementalLink": "",
            },
            {
                "name": "password",
                "defaultValue": "",
                "type": "password",
                "required": True,
                "label": "Password",
                "placeholder": "",
                "instructions": "",
                "restrictions": {
                    "max_length": DEFAULT_MAX_PASSWORD_LENGTH,
                },
                "errorMessages": {},
                "supplementalText": "",
                "supplementalLink": "",
            },
        ])

    def test_login(self):
        # Create a test user
        UserFactory.create(username=self.USERNAME, email=self.EMAIL, password=self.PASSWORD)

        # Login
        response = self.client.post(self.url, {
            "email": self.EMAIL,
            "password": self.PASSWORD,
        })
        self.assertHttpOK(response)

        # Verify that we logged in successfully by accessing
        # a page that requires authentication.
        response = self.client.get(reverse("dashboard"))
        self.assertHttpOK(response)

    def test_session_cookie_expiry(self):
        # Create a test user
        UserFactory.create(username=self.USERNAME, email=self.EMAIL, password=self.PASSWORD)

        # Login and remember me
        data = {
            "email": self.EMAIL,
            "password": self.PASSWORD,
        }

        response = self.client.post(self.url, data)
        self.assertHttpOK(response)

        # Verify that the session expiration was set correctly
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME]
        expected_expiry = datetime.datetime.utcnow() + datetime.timedelta(weeks=4)
        self.assertIn(expected_expiry.strftime('%d-%b-%Y'), cookie.get('expires'))

    def test_invalid_credentials(self):
        # Create a test user
        UserFactory.create(username=self.USERNAME, email=self.EMAIL, password=self.PASSWORD)

        # Invalid password
        response = self.client.post(self.url, {
            "email": self.EMAIL,
            "password": "invalid"
        })
        self.assertHttpForbidden(response)

        # Invalid email address
        response = self.client.post(self.url, {
            "email": "invalid@example.com",
            "password": self.PASSWORD,
        })
        self.assertHttpForbidden(response)

    def test_missing_login_params(self):
        # Create a test user
        UserFactory.create(username=self.USERNAME, email=self.EMAIL, password=self.PASSWORD)

        # Missing password
        response = self.client.post(self.url, {
            "email": self.EMAIL,
        })
        self.assertHttpBadRequest(response)

        # Missing email
        response = self.client.post(self.url, {
            "password": self.PASSWORD,
        })
        self.assertHttpBadRequest(response)

        # Missing both email and password
        response = self.client.post(self.url, {})


@ddt.ddt
class StudentViewShimTest(TestCase):
    "Tests of the student view shim."
    def setUp(self):
        super(StudentViewShimTest, self).setUp()
        self.captured_request = None

    def test_strip_enrollment_action(self):
        view = self._shimmed_view(HttpResponse())
        request = HttpRequest()
        request.POST["enrollment_action"] = "enroll"
        request.POST["course_id"] = "edx/101/demo"
        view(request)

        # Expect that the enrollment action and course ID
        # were stripped out before reaching the wrapped view.
        self.assertNotIn("enrollment_action", self.captured_request.POST)
        self.assertNotIn("course_id", self.captured_request.POST)

    def test_include_analytics_info(self):
        view = self._shimmed_view(HttpResponse())
        request = HttpRequest()
        request.POST["analytics"] = json.dumps({
            "enroll_course_id": "edX/DemoX/Fall"
        })
        view(request)

        # Expect that the analytics course ID was passed to the view
        self.assertEqual(self.captured_request.POST.get("course_id"), "edX/DemoX/Fall")

    def test_third_party_auth_login_failure(self):
        view = self._shimmed_view(
            HttpResponse(status=403),
            check_logged_in=True
        )
        response = view(HttpRequest())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"third-party-auth")

    def test_non_json_response(self):
        view = self._shimmed_view(HttpResponse(content="Not a JSON dict"))
        response = view(HttpRequest())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Not a JSON dict")

    @ddt.data("redirect", "redirect_url")
    def test_ignore_redirect_from_json(self, redirect_key):
        view = self._shimmed_view(
            HttpResponse(content=json.dumps({
                "success": True,
                redirect_key: "/redirect"
            }))
        )
        response = view(HttpRequest())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode('utf-8'), "")

    def test_error_from_json(self):
        view = self._shimmed_view(
            HttpResponse(content=json.dumps({
                "success": False,
                "value": "Error!"
            }))
        )
        response = view(HttpRequest())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Error!")

    def test_preserve_headers(self):
        view_response = HttpResponse()
        view_response["test-header"] = "test"
        view = self._shimmed_view(view_response)
        response = view(HttpRequest())
        self.assertEqual(response["test-header"], "test")

    def test_check_logged_in(self):
        view = self._shimmed_view(HttpResponse(), check_logged_in=True)
        response = view(HttpRequest())
        self.assertEqual(response.status_code, 403)

    def _shimmed_view(self, response, check_logged_in=False):
        def stub_view(request):
            self.captured_request = request
            return response
        return shim_student_view(stub_view, check_logged_in=check_logged_in)
