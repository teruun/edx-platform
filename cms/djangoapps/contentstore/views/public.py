"""
Public views
"""
from __future__ import absolute_import

from django.conf import settings
from django.shortcuts import redirect
from django.utils.http import urlquote_plus
from waffle.decorators import waffle_switch

from contentstore.config import waffle
from edxmako.shortcuts import render_to_response

__all__ = ['login_redirect_to_lms', 'howitworks', 'accessibility']


def register_redirect_to_lms(request):
    """
    This view redirects to the LMS register view. It is used to temporarily keep the old
    Studio signup url alive.
    """
    next_url = request.GET.get('next')
    absolute_next_url = request.build_absolute_uri(next_url)
    register_url = '{base_url}/register{params}'.format(
        base_url=settings.LMS_ROOT_URL,
        params='?next=' + urlquote_plus(absolute_next_url) if next_url else '',
    )
    return redirect(register_url, permanent=True)


def login_redirect_to_lms(request):
    """
    This view redirects to the LMS login view. It is used for Django's LOGIN_URL
    setting, which is where unauthenticated requests to protected endpoints are redirected.
    """
    next_url = request.GET.get('next')
    absolute_next_url = request.build_absolute_uri(next_url)
    login_url = '{base_url}/login{params}'.format(
        base_url=settings.LMS_ROOT_URL,
        params='?next=' + urlquote_plus(absolute_next_url) if next_url else '',
    )
    return redirect(login_url)


def howitworks(request):
    "Proxy view"
    if request.user.is_authenticated:
        return redirect('/home/')
    else:
        return render_to_response('howitworks.html', {})


@waffle_switch('{}.{}'.format(waffle.WAFFLE_NAMESPACE, waffle.ENABLE_ACCESSIBILITY_POLICY_PAGE))
def accessibility(request):
    """
    Display the accessibility accommodation form.
    """

    return render_to_response('accessibility.html', {
        'language_code': request.LANGUAGE_CODE
    })
