"""
Views for login / logout and associated functionality

Much of this file was broken out from views.py, previous history can be found there.
"""

from __future__ import absolute_import

from functools import wraps
import json
import logging

import six
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from edx_django_utils.monitoring import set_custom_metric
from ratelimitbackend.exceptions import RateLimitException
from rest_framework.views import APIView

from edxmako.shortcuts import render_to_response
from openedx.core.djangoapps.password_policy import compliance as password_policy_compliance
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_authn.views.login_form import get_login_session_form
from openedx.core.djangoapps.user_authn.cookies import refresh_jwt_cookies, set_logged_in_cookies
from openedx.core.djangoapps.user_authn.exceptions import AuthFailedError
from openedx.core.djangoapps.util.user_messages import PageLevelMessages
from openedx.core.djangoapps.user_authn.views.password_reset import send_password_reset_email_for_user
from openedx.core.djangoapps.user_authn.config.waffle import (
    ENABLE_LOGIN_USING_THIRDPARTY_AUTH_ONLY,
    UPDATE_LOGIN_USER_ERROR_STATUS_CODE
)
from openedx.core.djangolib.markup import HTML, Text
from openedx.core.lib.api.view_utils import require_post_params
from student.models import LoginFailures, AllowedAuthUser, UserProfile
from student.views import compose_and_send_activation_email
from third_party_auth import pipeline, provider
import third_party_auth
from track import segment
from util.json_request import JsonResponse
from util.password_policy_validators import normalize_password

log = logging.getLogger("edx.student")
AUDIT_LOG = logging.getLogger("audit")


def _do_third_party_auth(request):
    """
    User is already authenticated via 3rd party, now try to find and return their associated Django user.
    """
    running_pipeline = pipeline.get(request)
    username = running_pipeline['kwargs'].get('username')
    backend_name = running_pipeline['backend']
    third_party_uid = running_pipeline['kwargs']['uid']
    requested_provider = provider.Registry.get_from_pipeline(running_pipeline)
    platform_name = configuration_helpers.get_value("platform_name", settings.PLATFORM_NAME)

    try:
        return pipeline.get_authenticated_user(requested_provider, username, third_party_uid)
    except User.DoesNotExist:
        AUDIT_LOG.info(
            u"Login failed - user with username {username} has no social auth "
            u"with backend_name {backend_name}".format(
                username=username, backend_name=backend_name)
        )
        message = Text(_(
            u"You've successfully signed in to your {provider_name} account, "
            u"but this account isn't linked with your {platform_name} account yet. {blank_lines}"
            u"Use your {platform_name} username and password to sign in to {platform_name} below, "
            u"and then link your {platform_name} account with {provider_name} from your dashboard. {blank_lines}"
            u"If you don't have an account on {platform_name} yet, "
            u"click {register_label_strong} at the top of the page."
        )).format(
            blank_lines=HTML('<br/><br/>'),
            platform_name=platform_name,
            provider_name=requested_provider.name,
            register_label_strong=HTML('<strong>{register_text}</strong>').format(
                register_text=_('Register')
            )
        )

        raise AuthFailedError(message)


def _get_user_by_email(request):
    """
    Finds a user object in the database based on the given request, ignores all fields except for email.
    """
    if 'email' not in request.POST or 'password' not in request.POST:
        raise AuthFailedError(_('There was an error receiving your login information. Please email us.'))

    email = request.POST['email']

    try:
        return User.objects.get(email=email)
    except User.DoesNotExist:
        if settings.FEATURES['SQUELCH_PII_IN_LOGS']:
            AUDIT_LOG.warning(u"Login failed - Unknown user email")
        else:
            AUDIT_LOG.warning(u"Login failed - Unknown user email: {0}".format(email))


def _check_excessive_login_attempts(user):
    """
    See if account has been locked out due to excessive login failures
    """
    if user and LoginFailures.is_feature_enabled():
        if LoginFailures.is_user_locked_out(user):
            raise AuthFailedError(_('This account has been temporarily locked due '
                                    'to excessive login failures. Try again later.'))


def _enforce_password_policy_compliance(request, user):
    try:
        password_policy_compliance.enforce_compliance_on_login(user, request.POST.get('password'))
    except password_policy_compliance.NonCompliantPasswordWarning as e:
        # Allow login, but warn the user that they will be required to reset their password soon.
        PageLevelMessages.register_warning_message(request, six.text_type(e))
    except password_policy_compliance.NonCompliantPasswordException as e:
        send_password_reset_email_for_user(user, request)
        # Prevent the login attempt.
        raise AuthFailedError(HTML(six.text_type(e)))


def _generate_not_activated_message(user):
    """
    Generates the message displayed on the sign-in screen when a learner attempts to access the
    system with an inactive account.
    """

    support_url = configuration_helpers.get_value(
        'SUPPORT_SITE_LINK',
        settings.SUPPORT_SITE_LINK
    )

    platform_name = configuration_helpers.get_value(
        'PLATFORM_NAME',
        settings.PLATFORM_NAME
    )
    not_activated_message = Text(_(
        u'In order to sign in, you need to activate your account.{blank_lines}'
        u'We just sent an activation link to {email_strong}. If '
        u'you do not receive an email, check your spam folders or '
        u'{link_start}contact {platform_name} Support{link_end}.'
    )).format(
        platform_name=platform_name,
        blank_lines=HTML('<br/><br/>'),
        email_strong=HTML('<strong>{email}</strong>').format(email=user.email),
        link_start=HTML(u'<a href="{support_url}">').format(
            support_url=support_url,
        ),
        link_end=HTML("</a>"),
    )

    return not_activated_message


def _log_and_raise_inactive_user_auth_error(unauthenticated_user):
    """
    Depending on Django version we can get here a couple of ways, but this takes care of logging an auth attempt
    by an inactive user, re-sending the activation email, and raising an error with the correct message.
    """
    if settings.FEATURES['SQUELCH_PII_IN_LOGS']:
        AUDIT_LOG.warning(
            u"Login failed - Account not active for user.id: {0}, resending activation".format(
                unauthenticated_user.id)
        )
    else:
        AUDIT_LOG.warning(u"Login failed - Account not active for user {0}, resending activation".format(
            unauthenticated_user.username)
        )

    profile = UserProfile.objects.get(user=unauthenticated_user)
    compose_and_send_activation_email(unauthenticated_user, profile)

    raise AuthFailedError(_generate_not_activated_message(unauthenticated_user))


def _authenticate_first_party(request, unauthenticated_user):
    """
    Use Django authentication on the given request, using rate limiting if configured
    """

    # If the user doesn't exist, we want to set the username to an invalid username so that authentication is guaranteed
    # to fail and we can take advantage of the ratelimited backend
    username = unauthenticated_user.username if unauthenticated_user else ""

    _check_user_auth_flow(request.site, unauthenticated_user)

    try:
        password = normalize_password(request.POST['password'])
        return authenticate(
            username=username,
            password=password,
            request=request
        )

    # This occurs when there are too many attempts from the same IP address
    except RateLimitException:
        raise AuthFailedError(_('Too many failed login attempts. Try again later.'))


def _handle_failed_authentication(user, authenticated_user):
    """
    Handles updating the failed login count, inactive user notifications, and logging failed authentications.
    """
    if user:
        if LoginFailures.is_feature_enabled():
            LoginFailures.increment_lockout_counter(user)

        if authenticated_user and not user.is_active:
            _log_and_raise_inactive_user_auth_error(user)

        # if we didn't find this username earlier, the account for this email
        # doesn't exist, and doesn't have a corresponding password
        if settings.FEATURES['SQUELCH_PII_IN_LOGS']:
            loggable_id = user.id if user else "<unknown>"
            AUDIT_LOG.warning(u"Login failed - password for user.id: {0} is invalid".format(loggable_id))
        else:
            AUDIT_LOG.warning(u"Login failed - password for {0} is invalid".format(user.email))

    raise AuthFailedError(_('Email or password is incorrect.'))


def _handle_successful_authentication_and_login(user, request):
    """
    Handles clearing the failed login counter, login tracking, and setting session timeout.
    """
    if LoginFailures.is_feature_enabled():
        LoginFailures.clear_lockout_counter(user)

    _track_user_login(user, request)

    try:
        django_login(request, user)
        request.session.set_expiry(604800 * 4)
        log.debug("Setting user session expiry to 4 weeks")
    except Exception as exc:
        AUDIT_LOG.critical("Login failed - Could not create session. Is memcached running?")
        log.critical("Login failed - Could not create session. Is memcached running?")
        log.exception(exc)
        raise


def _track_user_login(user, request):
    """
    Sends a tracking event for a successful login.
    """
    # .. pii: Username and email are sent to Segment here. Retired directly through Segment API call in Tubular.
    # .. pii_types: email_address, username
    # .. pii_retirement: third_party
    segment.identify(
        user.id,
        {
            'email': request.POST.get('email'),
            'username': user.username
        },
        {
            # Disable MailChimp because we don't want to update the user's email
            # and username in MailChimp on every page load. We only need to capture
            # this data on registration/activation.
            'MailChimp': False
        }
    )
    segment.track(
        user.id,
        "edx.bi.user.account.authenticated",
        {
            'category': "conversion",
            'label': request.POST.get('course_id'),
            'provider': None
        },
    )


def _check_user_auth_flow(site, user):
    """
    Check if user belongs to an allowed domain and not whitelisted
    then ask user to login through allowed domain SSO provider.
    """
    if user and ENABLE_LOGIN_USING_THIRDPARTY_AUTH_ONLY.is_enabled():
        allowed_domain = site.configuration.get_value('THIRD_PARTY_AUTH_ONLY_DOMAIN', '').lower()
        user_domain = user.email.split('@')[1].strip().lower()

        # If user belongs to allowed domain and not whitelisted then user must login through allowed domain SSO
        if user_domain == allowed_domain and not AllowedAuthUser.objects.filter(site=site, email=user.email).exists():
            msg = _(
                u'As an {allowed_domain} user, You must login with your {allowed_domain} {provider} account.'
            ).format(
                allowed_domain=allowed_domain,
                provider=site.configuration.get_value('THIRD_PARTY_AUTH_ONLY_PROVIDER')
            )
            raise AuthFailedError(msg)


@login_required
@require_http_methods(['GET'])
def finish_auth(request):  # pylint: disable=unused-argument
    """ Following logistration (1st or 3rd party), handle any special query string params.

    See FinishAuthView.js for details on the query string params.

    e.g. auto-enroll the user in a course, set email opt-in preference.

    This view just displays a "Please wait" message while AJAX calls are made to enroll the
    user in the course etc. This view is only used if a parameter like "course_id" is present
    during login/registration/third_party_auth. Otherwise, there is no need for it.

    Ideally this view will finish and redirect to the next step before the user even sees it.

    Args:
        request (HttpRequest)

    Returns:
        HttpResponse: 200 if the page was sent successfully
        HttpResponse: 302 if not logged in (redirect to login page)
        HttpResponse: 405 if using an unsupported HTTP method

    Example usage:

        GET /account/finish_auth/?course_id=course-v1:blah&enrollment_action=enroll

    """
    return render_to_response('student_account/finish_auth.html', {
        'disable_courseware_js': True,
        'disable_footer': True,
    })


@ensure_csrf_cookie
def login_user(request):
    """
    AJAX request to log in the user.
    """
    third_party_auth_requested = third_party_auth.is_enabled() and pipeline.running(request)
    first_party_auth_requested = bool(request.POST.get('email')) or bool(request.POST.get('password'))
    is_user_third_party_authenticated = False

    set_custom_metric('login_user_enrollment_action', request.POST.get('enrollment_action'))
    set_custom_metric('login_user_course_id', request.POST.get('course_id'))

    try:
        if third_party_auth_requested and not first_party_auth_requested:
            # The user has already authenticated via third-party auth and has not
            # asked to do first party auth by supplying a username or password. We
            # now want to put them through the same logging and cookie calculation
            # logic as with first-party auth.

            # This nested try is due to us only returning an HttpResponse in this
            # one case vs. JsonResponse everywhere else.
            try:
                user = _do_third_party_auth(request)
                is_user_third_party_authenticated = True
                set_custom_metric('login_user_tpa_success', True)
            except AuthFailedError as e:
                set_custom_metric('login_user_tpa_success', False)
                set_custom_metric('login_user_tpa_failure_msg', e.value)
                return HttpResponse(e.value, content_type="text/plain", status=403)
        else:
            user = _get_user_by_email(request)

        _check_excessive_login_attempts(user)

        possibly_authenticated_user = user

        if not is_user_third_party_authenticated:
            possibly_authenticated_user = _authenticate_first_party(request, user)
            if possibly_authenticated_user and password_policy_compliance.should_enforce_compliance_on_login():
                # Important: This call must be made AFTER the user was successfully authenticated.
                _enforce_password_policy_compliance(request, possibly_authenticated_user)

        if possibly_authenticated_user is None or not possibly_authenticated_user.is_active:
            _handle_failed_authentication(user, possibly_authenticated_user)

        _handle_successful_authentication_and_login(possibly_authenticated_user, request)

        redirect_url = None  # The AJAX method calling should know the default destination upon success
        if is_user_third_party_authenticated:
            running_pipeline = pipeline.get(request)
            redirect_url = pipeline.get_complete_url(backend_name=running_pipeline['backend'])

        response = JsonResponse({
            'success': True,
            'redirect_url': redirect_url,
        })

        # Ensure that the external marketing site can
        # detect that the user is logged in.
        response = set_logged_in_cookies(request, response, possibly_authenticated_user)
        set_custom_metric('login_user_auth_failed_error', False)
        set_custom_metric('login_user_response_status', response.status_code)
        return response
    except AuthFailedError as error:
        log.exception(error.get_response())
        # original code returned a 200 status code with status=False for errors. This flag
        # is used for rolling out a transition to using a 400 status code for errors, which
        # is a breaking-change, but will hopefully be a tolerable breaking-change.
        status = 400 if UPDATE_LOGIN_USER_ERROR_STATUS_CODE.is_enabled() else 200
        response = JsonResponse(error.get_response(), status=status)
        set_custom_metric('login_user_auth_failed_error', True)
        set_custom_metric('login_user_response_status', response.status_code)
        return response


# CSRF protection is not needed here because the only side effect
# of this endpoint is to refresh the cookie-based JWT, and attempting
# to get a CSRF token before we need to refresh adds too much
# complexity.
@csrf_exempt
@require_http_methods(['POST'])
def login_refresh(request):
    if not request.user.is_authenticated or request.user.is_anonymous:
        return JsonResponse('Unauthorized', status=401)

    try:
        response = JsonResponse({'success': True})
        return refresh_jwt_cookies(request, response, request.user)
    except AuthFailedError as error:
        log.exception(error.get_response())
        return JsonResponse(error.get_response(), status=400)


class LoginSessionView(APIView):
    """HTTP end-points for logging in users. """

    # This end-point is available to anonymous users,
    # so do not require authentication.
    authentication_classes = []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return HttpResponse(get_login_session_form(request).to_json(), content_type="application/json")

    @method_decorator(require_post_params(["email", "password"]))
    @method_decorator(csrf_protect)
    def post(self, request):
        """Log in a user.

        You must send all required form fields with the request.

        You can optionally send an `analytics` param with a JSON-encoded
        object with additional info to include in the login analytics event.
        Currently, the only supported field is "enroll_course_id" to indicate
        that the user logged in while enrolling in a particular course.

        Arguments:
            request (HttpRequest)

        Returns:
            HttpResponse: 200 on success
            HttpResponse: 400 if the request is not valid.
            HttpResponse: 403 if authentication failed.
                403 with content "third-party-auth" if the user
                has successfully authenticated with a third party provider
                but does not have a linked account.
            HttpResponse: 302 if redirecting to another page.

        Example Usage:

            POST /user_api/v1/login_session
            with POST params `email`, `password`, and `remember`.

            200 OK

        """
        return shim_student_view(login_user, check_logged_in=True)(request)

    @method_decorator(sensitive_post_parameters("password"))
    def dispatch(self, request, *args, **kwargs):
        return super(LoginSessionView, self).dispatch(request, *args, **kwargs)


def shim_student_view(view_func, check_logged_in=False):
    """Create a "shim" view for a view function from the student Django app.

    UPDATE: This shim is only used to wrap `login_user`, which now lives in
    the user_authn Django app (not the student app).

    Specifically, we need to:
    * Strip out enrollment params, since the client for the new registration/login
        page will communicate with the enrollment API to update enrollments.

    * Return responses with HTTP status codes indicating success/failure
        (instead of always using status 200, but setting "success" to False in
        the JSON-serialized content of the response)

    * Use status code 403 to indicate a login failure.

    The shim will preserve any cookies set by the view.

    Arguments:
        view_func (function): The view function from the student Django app.

    Keyword Args:
        check_logged_in (boolean): If true, check whether the user successfully
            authenticated and if not set the status to 403.

    Returns:
        function

    """
    @wraps(view_func)
    def _inner(request):  # pylint: disable=missing-docstring
        # Make a copy of the current POST request to modify.
        modified_request = request.POST.copy()
        if isinstance(request, HttpRequest):
            # Works for an HttpRequest but not a rest_framework.request.Request.
            set_custom_metric('shim_request_type', 'traditional')
            request.POST = modified_request
        else:
            set_custom_metric('shim_request_type', 'drf')
            # The request must be a rest_framework.request.Request.
            request._data = modified_request  # pylint: disable=protected-access

        # The login and registration handlers in student view try to change
        # the user's enrollment status if these parameters are present.
        # Since we want the JavaScript client to communicate directly with
        # the enrollment API, we want to prevent the student views from
        # updating enrollments.
        if "enrollment_action" in modified_request:
            set_custom_metric('shim_del_enrollment_action', modified_request["enrollment_action"])
            del modified_request["enrollment_action"]
        if "course_id" in modified_request:
            set_custom_metric('shim_del_course_id', modified_request["course_id"])
            del modified_request["course_id"]

        # Include the course ID if it's specified in the analytics info
        # so it can be included in analytics events.
        if "analytics" in modified_request:
            try:
                analytics = json.loads(modified_request["analytics"])
                if "enroll_course_id" in analytics:
                    set_custom_metric('shim_analytics_course_id', analytics.get("enroll_course_id"))
                    modified_request["course_id"] = analytics.get("enroll_course_id")
            except (ValueError, TypeError):
                set_custom_metric('shim_analytics_course_id', 'parse-error')
                log.error(
                    u"Could not parse analytics object sent to user API: {analytics}".format(
                        analytics=analytics
                    )
                )

        # Call the original view to generate a response.
        # We can safely modify the status code or content
        # of the response, but to be safe we won't mess
        # with the headers.
        response = view_func(request)

        # Most responses from this view are JSON-encoded
        # dictionaries with keys "success", "value", and
        # (sometimes) "redirect_url".
        #
        # We want to communicate some of this information
        # using HTTP status codes instead.
        #
        # We ignore the "redirect_url" parameter, because we don't need it:
        # 1) It's used to redirect on change enrollment, which
        # our client will handle directly
        # (that's why we strip out the enrollment params from the request)
        # 2) It's used by third party auth when a user has already successfully
        # authenticated and we're not sending login credentials.  However,
        # this case is never encountered in practice: on the old login page,
        # the login form would be submitted directly, so third party auth
        # would always be "trumped" by first party auth.  If a user has
        # successfully authenticated with us, we redirect them to the dashboard
        # regardless of how they authenticated; and if a user is completing
        # the third party auth pipeline, we redirect them from the pipeline
        # completion end-point directly.
        try:
            response_dict = json.loads(response.content.decode('utf-8'))
            msg = response_dict.get("value", u"")
            success = response_dict.get("success")
            set_custom_metric('shim_original_response_is_json', True)
        except (ValueError, TypeError):
            msg = response.content
            success = True
            set_custom_metric('shim_original_response_is_json', False)
        set_custom_metric('shim_original_response_msg', msg)
        set_custom_metric('shim_original_response_success', success)
        set_custom_metric('shim_original_response_status', response.status_code)

        # If the user is not authenticated when we expect them to be
        # send the appropriate status code.
        # We check whether the user attribute is set to make
        # it easier to test this without necessarily running
        # the request through authentication middleware.
        is_authenticated = (
            getattr(request, 'user', None) is not None
            and request.user.is_authenticated
        )
        if check_logged_in and not is_authenticated:
            # If we get a 403 status code from the student view
            # this means we've successfully authenticated with a
            # third party provider, but we don't have a linked
            # EdX account.  Send a helpful error code so the client
            # knows this occurred.
            if response.status_code == 403:
                response.content = "third-party-auth"

            # Otherwise, it's a general authentication failure.
            # Ensure that the status code is a 403 and pass
            # along the message from the view.
            else:
                response.status_code = 403
                response.content = msg

        # If an error condition occurs, send a status 400
        elif response.status_code != 200 or not success:
            # login_user sends status 200 even when an error occurs
            # If the JSON-serialized content has a value "success" set to False,
            # then we know an error occurred.
            # NOTE: temporary metric added so we can remove this code once the
            # original response is 400 instead of 200.
            set_custom_metric('shim_adjusted_status_code', bool(response.status_code == 200))
            if response.status_code == 200:
                response.status_code = 400
            response.content = msg

        # If the response is successful, then return the content
        # of the response directly rather than including it
        # in a JSON-serialized dictionary.
        else:
            response.content = msg

        set_custom_metric('shim_final_response_msg', response.content)
        set_custom_metric('shim_final_response_status', response.status_code)
        # Return the response, preserving the original headers.
        # This is really important, since the student views set cookies
        # that are used elsewhere in the system (such as the marketing site).
        return response

    return _inner
