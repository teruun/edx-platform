"""
Tests for courseware services.
"""
from __future__ import absolute_import

import ddt
import json

from lms.djangoapps.courseware.services import StudentModuleService
from lms.djangoapps.courseware.tests.factories import StudentModuleFactory, UserFactory
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory, ItemFactory


@ddt.ddt
class TestStudentModuleService(ModuleStoreTestCase):
    """
    Test suite for csm service.
    """

    def setUp(self):
        """
        Creating pre-requisites for the test cases.
        """
        super(TestStudentModuleService, self).setUp()
        self.user = UserFactory.create()
        self.course = CourseFactory.create()
        chapter = ItemFactory.create(
            category='chapter',
            parent=self.course,
            display_name='Test Chapter'
        )
        sequential = ItemFactory.create(
            category='sequential',
            parent=chapter,
            display_name='Test Sequential'
        )
        vertical = ItemFactory.create(
            category='vertical',
            parent=sequential,
            display_name='Test Vertical'
        )
        self.problem = ItemFactory.create(
            category='problem',
            parent=vertical,
            display_name='Test Problem'
        )

    def _create_student_module(self, state):
        StudentModuleFactory.create(
            student=self.user,
            module_state_key=self.problem.location,
            course_id=self.course.id,
            state=json.dumps(state)
        )

    @ddt.data(
        ({'key_1a': 'value_1a', 'key_2a': 'value_2a'}),
        ({'key_1b': 'value_1b', 'key_2b': 'value_2b'})
    )
    def test_student_state(self, expected_state):
        """
        Verify the services get the correct state from the CSM.

        Scenario:
            Given a user and a problem/block
            Then create a student module entry for the user
            If the state is obtained from student module service
            Then the state is equal to previously created CSM state
        """
        self._create_student_module(expected_state)
        state = StudentModuleService().get_state_as_json(
            self.user.username, self.problem.location
        )
        self.assertDictEqual(state, expected_state)
