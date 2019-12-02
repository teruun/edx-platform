"""
Courseware services
"""
from __future__ import absolute_import

import json

from lms.djangoapps.courseware.models import StudentModule


class StudentModuleService(object):
    """
    Student Module service to make SM accessible in runtime.

    Provides methods to access student module object for different criteria.
    """

    def get_state_as_json(self, username, block_id):
        """
        Return user state as json for given parameters.

        Arguments:
            username: username of the user for whom the data is being retrieved
            block_id: string/object representation of the block whose user state is required

        Returns:
            user state in form of json is returned. Empty json is returned
            if object isn't found.

        """
        try:
            student_module = StudentModule.objects.get(
                student__username=username,
                module_state_key=block_id
            )
        except StudentModule.DoesNotExist:
            student_module = {}

        if student_module:
            return json.loads(student_module.state)
        return student_module
