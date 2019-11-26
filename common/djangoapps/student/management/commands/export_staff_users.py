from __future__ import absolute_import, print_function

import logging
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.conf import settings
from django.core.mail.message import EmailMultiAlternatives
from django.template.loader import get_template
from os import remove

from djqscsv import write_csv
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from student.models import CourseAccessRole

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Example usage:
        $ ./manage.py lms export_staff_users 7  --settings=devstack_docker
    """

    help = """
    This command will export a csv of all users who have logged in within the given days and
    have staff access role in active courses (Courses with end date in the future).
    """

    def add_arguments(self, parser):
        parser.add_argument(
            'days',
            type=int,
            default=7,
            help='Indicate the login time period in days starting from today'
        )

    subject = 'Staff users CSV'
    to_addresses = ['aazam@edx.org']
    from_address = settings.DEFAULT_FROM_EMAIL
    txt_template_path = 'email/export_staff_users.txt'
    html_template_path = 'email/export_staff_users.html'
    csv_filename = 'staff_users.csv'

    def handle(self, *args, **kwargs):
        days = kwargs['days']
        current_date = datetime.now()
        starting_date = current_date - timedelta(days=days)
        active_courses = CourseOverview.objects.filter(end__gte=current_date).values_list('id', flat=True)
        course_access_roles = CourseAccessRole.objects.filter(
            role__in=['staff', 'instructor'],
            user__last_login__range=(current_date, starting_date),
            course_id__in=active_courses,
            user__is_staff=False
        ).values('user__username', 'user__email', 'role')
        try:
            self.send_email(course_access_roles, days)
            logger.info(
                'Sent staff users email for the period {} to {}. Staff users count:{}'.format(
                    starting_date,
                    current_date,
                    course_access_roles.count()
                )
            )
        except Exception:
            logger.exception(
                'Failed to send staff users email for the period {}-{}'.format(starting_date, current_date)
            )

        return course_access_roles.count()

    def send_email(self, user_data, days):
        """
        Sends an email to admin containing a csv of all users who have logged in within the given days and
        have staff access role in active courses (Courses with end date in the future).
        :param user_data:
        :param days:
        :return:
        """
        context = {'time_period': days}
        plain_content = self.render_template(self.txt_template_path, context)
        html_content = self.render_template(self.html_template_path, context)

        with open(self.csv_filename, 'a+') as csv_file:
            write_csv(user_data, csv_file)
            email_message = EmailMultiAlternatives(self.subject, plain_content, self.from_address, to=self.to_addresses)
            email_message.attach_alternative(html_content, 'text/html')
            email_message.attach(self.csv_filename, csv_file.read(), 'text/csv')
            email_message.send()

        remove(self.csv_filename)

    def render_template(self, path, context):
        """
        Takes a template path and context and returns a rendered template
        :param path:
        :param context:
        :return:
        """
        txt_template = get_template(path)
        return txt_template.render(context)
