"""
Unit tests for export_staff_users management command.
"""
from datetime import timedelta

from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils.timezone import now

from openedx.core.djangoapps.content.course_overviews.tests.factories import CourseOverviewFactory
from student.tests.factories import CourseAccessRoleFactory


class TestExportStaffUsers(TestCase):
    """
    Tests the `export_staff_users` command.
    """

    def create_users_data(self):
        course = CourseOverviewFactory(end=now() + timedelta(days=30))
        archived_course = CourseOverviewFactory(end=now() - timedelta(days=30))
        CourseAccessRoleFactory.create(
            course_id=course.id, user=self.user, role="instructor",
        )
        CourseAccessRoleFactory.create(
            course_id=course.id, user=self.user, role="staff",
        )
        CourseAccessRoleFactory.create(
            course_id=archived_course.id, user=self.user, role="instructor",
        )
        CourseAccessRoleFactory.create(
            course_id=archived_course.id, user=self.user, role="staff",
        )

    def test_export_staff_users(self):
        self.create_users_data()
        self.assertEqual(len(mail.outbox), 0)
        count = call_command('export_staff_users', 7)
        self.assertEqual(count, 2)
        self.assertEqual(len(mail.outbox), 1)

