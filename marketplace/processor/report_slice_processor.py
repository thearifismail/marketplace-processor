#
# Copyright 2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Report Slice Processor."""

import asyncio
import json
import logging
import threading

from processor.abstract_processor import (AbstractProcessor, FAILED_TO_VALIDATE)
from processor.processor_utils import (PROCESSOR_INSTANCES,
                                       SLICE_PROCESSING_LOOP,
                                       format_message)
from processor.report_consumer import (MKTReportException,)

from api.models import ReportSlice
from api.serializers import ReportSliceSerializer
from config.settings.base import (RETRIES_ALLOWED,
                                  RETRY_TIME)

LOG = logging.getLogger(__name__)

FAILED_UPLOAD = 'UPLOAD'
RETRIES_ALLOWED = int(RETRIES_ALLOWED)
RETRY_TIME = int(RETRY_TIME)
UPLOAD_TOPIC = 'platform.inventory.host-ingress'  # placeholder topic


class RetryUploadTimeException(Exception):
    """Use to report upload errors that should be retried on time."""

    pass


class RetryUploadCommitException(Exception):
    """Use to report upload errors that should be retried on commit."""

    pass


class ReportSliceProcessor(AbstractProcessor):  # pylint: disable=too-many-instance-attributes
    """Class for processing report slices that have been created."""

    def __init__(self):
        """Create a report slice state machine."""
        state_functions = {
            ReportSlice.RETRY_VALIDATION: self.transition_to_validated,
            ReportSlice.NEW: self.transition_to_started,
            ReportSlice.STARTED: self.transition_to_upload_object_storage,
            ReportSlice.VALIDATED: self.transition_to_upload_object_storage,
            ReportSlice.METRICS_UPLOADED: self.archive_report_and_slices,
            ReportSlice.FAILED_VALIDATION: self.archive_report_and_slices,
            ReportSlice.FAILED_METRICS_UPLOAD: self.archive_report_and_slices}
        state_metrics = {
            ReportSlice.FAILED_VALIDATION: FAILED_TO_VALIDATE
        }
        super().__init__(pre_delegate=self.pre_delegate,
                         state_functions=state_functions,
                         state_metrics=state_metrics,
                         async_states=[ReportSlice.STARTED, ReportSlice.VALIDATED],
                         object_prefix='REPORT SLICE',
                         object_class=ReportSlice,
                         object_serializer=ReportSliceSerializer
                         )

    def pre_delegate(self):
        """Call the correct function based on report slice state.

        If the function is async, make sure to await it.
        """
        self.state = self.report_or_slice.state
        self.account_number = self.report_or_slice.account
        if self.report_or_slice.report_json:
            self.report_json = json.loads(self.report_or_slice.report_json)
        if self.report_or_slice.report_platform_id:
            self.report_platform_id = self.report_or_slice.report_platform_id
        if self.report_or_slice.report_slice_id:
            self.report_slice_id = self.report_or_slice.report_slice_id

    def transition_to_validated(self):
        """Revalidate the slice because it is in the failed validation state."""
        self.prefix = 'ATTEMPTING VALIDATION'
        LOG.info(format_message(
            self.prefix,
            'Uploading data to Object Storage. State is "%s".' % self.report_or_slice.state,
            account_number=self.account_number, report_platform_id=self.report_platform_id))
        try:
            self.report_json = json.loads(self.report_or_slice.report_json)
            self._validate_report_details()
            # Here we want to update the report state of the actual report slice & when finished
            self.next_state = ReportSlice.VALIDATED
            self.update_object_state(options={})
        except MKTReportException:
            # if any MKTReportExceptions occur, we know that the report is not valid but has been
            # successfully validated
            # that means that this slice is invalid and only awaits being archived
            self.next_state = ReportSlice.FAILED_VALIDATION
            self.update_object_state(options={})
        except Exception as error:  # pylint: disable=broad-except
            # This slice blew up validation - we want to retry it later,
            # which means it enters our odd state
            # of requiring validation
            LOG.error(format_message(self.prefix, 'The following error occurred: %s.' % str(error)))
            self.determine_retry(ReportSlice.FAILED_VALIDATION, ReportSlice.RETRY_VALIDATION,
                                 retry_type=ReportSlice.GIT_COMMIT)

    async def transition_to_upload_object_storage(self):
        """Upload slice to object storage."""
        self.prefix = 'ATTEMPTING OBJECT STORAGE UPLOAD'
        LOG.info(format_message(
            self.prefix,
            'Uploading data to Object Storage. State is "%s".' % self.report_or_slice.state,
            account_number=self.account_number, report_platform_id=self.report_platform_id))

        try:
            await self._upload_to_object_storage()
            LOG.info(format_message(self.prefix, 'All metrics were successfully uploaded.',
                                    account_number=self.account_number,
                                    report_platform_id=self.report_platform_id))
            self.next_state = ReportSlice.METRICS_UPLOADED
            options = {'ready_to_archive': True}
            self.update_object_state(options=options)
        except Exception as error:  # pylint: disable=broad-except
            LOG.error(format_message(self.prefix, 'The following error occurred: %s.' % str(error),
                                     account_number=self.account_number,
                                     report_platform_id=self.report_platform_id))
            self.determine_retry(ReportSlice.FAILED_METRICS_UPLOAD, ReportSlice.VALIDATED,
                                 retry_type=ReportSlice.TIME)

    async def _upload_to_object_storage(self):
        """Upload to the metrics to object storage."""
        self.prefix = 'UPLOAD TO METRICS TO OBJECT STORAGE'
        LOG.info(
            format_message(
                self.prefix,
                'Sending %s metrics to object storage.' % (self.report_slice_id),
                account_number=self.account_number,
                report_platform_id=self.report_platform_id))
        await asyncio.sleep(1)


def asyncio_report_processor_thread(loop):  # pragma: no cover
    """
    Worker thread function to run the asyncio event loop.

    Creates a report processor and calls the run method.

    :param loop: event loop
    :returns None
    """
    processor = ReportSliceProcessor()
    PROCESSOR_INSTANCES.append(processor)
    try:
        loop.run_until_complete(processor.run())
    except Exception:  # pylint: disable=broad-except
        pass


def initialize_report_slice_processor():  # pragma: no cover
    """
    Create asyncio tasks and daemon thread to run event loop.

    Calls the report processor thread.

    :param None
    :returns None
    """
    event_loop_thread = threading.Thread(target=asyncio_report_processor_thread,
                                         args=(SLICE_PROCESSING_LOOP,))
    event_loop_thread.daemon = True
    event_loop_thread.start()