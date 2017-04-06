'''
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import json
import logging
import os
import ssl
import tempfile
import urllib2

from collections import namedtuple

import boto3
from botocore.exceptions import ClientError

logging.basicConfig()
LOGGER = logging.getLogger('StreamOutput')

OutputProperty = namedtuple('OutputProperty',
                            'description, value, is_secret, cred_requirement')
OutputProperty.__new__.__defaults__ = ('', '', False, False)


class OutputRequestFailure(Exception):
    """OutputRequestFailure handles any HTTP failures"""


class StreamOutputBase(object):
    """StreamOutputBase is the base class to handle routing alerts to outputs

    """
    __service__ = NotImplemented
    __config_service__ = __service__

    def __init__(self, region, s3_prefix):
        self.region = region
        self.s3_prefix = self._format_prefix(s3_prefix)

    @staticmethod
    def _local_temp_dir():
        """Get the local tmp directory for caching the encrypted service credentials

        Returns:
            [string] local path for stream_alert_secrets tmp directory
        """
        temp_dir = os.path.join(tempfile.gettempdir(), "stream_alert_secrets")

        # Check if this item exists as a file, and remove it if it does
        if os.path.exists(temp_dir) and not os.path.isdir(temp_dir):
            os.remove(temp_dir)

        # Create the folder on disk to store the credentials temporarily
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)

        return temp_dir

    def _load_creds(self, descriptor):
        """First try to load the credentials from /tmp and then resort to pulling
        the credentials from S3 if they are not cached locally

        Args:
            descriptor [string]: unique identifier used to look up these credentials

        Returns:
            [dict] the loaded credential info needed for sending alerts to this service
        """
        local_cred_location = os.path.join(self._local_temp_dir(),
                                           self.output_cred_name(descriptor))

        # Creds are not cached locally, so get the encrypted blob from s3
        if not os.path.exists(local_cred_location):
            if not self._get_creds_from_s3(local_cred_location, descriptor):
                return

        with open(local_cred_location, 'wb') as cred_file:
            enc_creds = cred_file.read()

        # Get the decrypted credential json from kms and load into dict
        # This could be None if the kms decryption fails, so check it
        decrypted_creds = self._kms_decrypt(enc_creds)
        if not decrypted_creds:
            return

        creds_dict = json.loads(decrypted_creds)

        # Add any of the hard-coded default output props to this dict (ie: url)
        defaults = self.get_default_properties()
        if defaults:
            creds_dict.update(defaults)

        return creds_dict

    def get_secrets_bucket_name(self):
        return self._format_s3_bucket('streamalert.secrets')

    def _format_s3_bucket(self, suffix):
        """Format the s3 bucket by combining the stored qualifier with a suffix

        Args:
            suffix [string]: Suffix for an s3 bucket

        Returns:
            [string] The combined prefix and suffix
        """
        return '.'.join([self.s3_prefix, suffix])

    def output_cred_name(self, descriptor):
        """Formats the output name for this credential by combining the service
        and the descriptor.

        Args:
            descriptor [string]: Service destination (ie: slack channel, pd integration)

        Return:
            [string] Formatted credential name (ie: slack_ryandchannel)
        """
        cred_name = str(self.__service__)

        # should descriptor be enforced in all rules?
        if descriptor:
            cred_name = '{}_{}'.format(cred_name, descriptor)

        return cred_name

    def _get_creds_from_s3(self, cred_location, descriptor):
        """Pull the encrypted credential blob for this service and destination from s3

        Args:
            cred_location [string]: The tmp path on disk to to store the encrypted blob
            descriptor [string]: Service destination (ie: slack channel, pd integration)

        Returns:
            [boolean] True if download of creds from s3 was a success
        """
        try:
            client = boto3.client('s3', region_name=self.region)
            with open(cred_location, 'wb') as cred_output:
                client.download_fileobj(self.get_secrets_bucket_name(),
                                        self.output_cred_name(descriptor),
                                        cred_output)

            return True
        except ClientError as err:
            LOGGER.error('credentials for %s could not be downloaded from S3: %s',
                         self.output_cred_name(descriptor),
                         err.response)

    def _kms_decrypt(self, data):
        """Decrypt data with AWS KMS.

        Args:
            data [string]: An encrypted ciphertext data blob

        Returns:
            [string] Decrypted json string
        """
        try:
            client = boto3.client('kms', region_name=self.region)
            response = client.decrypt(CiphertextBlob=data)
            return response['Plaintext']
        except ClientError as err:
            LOGGER.error('an error occurred during credentials decryption: %s', err.response)

    def _log_status(self, success):
        """Log the status of sending the alerts

        Args:
            success [boolean]: Indicates if the dispatching of alerts was successful
        """
        if success:
            LOGGER.info('successfully sent alert to %s', self.__service__)
        else:
            LOGGER.error('failed to send alert to %s', self.__service__)

    @staticmethod
    def _format_prefix(s3_prefix):
        """Return a bucket prefix that has been properly formatted

        Args:
            s3_prefix [string]: Qualifier value to format

        Returns:
            [string] The formatted value
        """
        s3_prefix = s3_prefix.replace('_streamalert_alert_processor', '')
        return s3_prefix.replace('_', '.')

    @staticmethod
    def _request_helper(url, data, headers=None, verify=True):
        """URL request helper to send a payload to an endpoint

        Args:
            url [string]: Endpoint for this request
            data [string]: Payload to send with this request
            headers [dict=None]: Dictionary containing request-specific header parameters
            verify [boolean=True]: Whether or not SSL should be used for this request
        Returns:
            [file handle] Contains the http response to be read
        """
        try:
            context = None
            if not verify:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            request = urllib2.Request(url, data=data, headers=headers)
            resp = urllib2.urlopen(request, context=context)
            return resp
        except urllib2.HTTPError as err:
            raise OutputRequestFailure('Failed to send to {} - [{}] {}'.format(err.url,
                                                                               err.code,
                                                                               err.read()))
    @staticmethod
    def _check_http_response(resp):
        return resp and (200 <= resp.getcode() <= 299)

    @classmethod
    def get_user_defined_properties(cls):
        """Base method for retrieving properties that must be asssigned by the user
        for a this output service integration. Overridden in output subclasses

        Returns:
            [OrderedDict] Contains various OutputProperty items
        """
        pass

    @classmethod
    def get_default_properties(cls):
        """Base method for retrieving properties that are hard coded for this
        output service integration. Overridden in output subclasses

        Returns:
            [OrderedDict] Contains various OutputProperty items
        """
        pass

    def get_config_service(self):
        """Get the string used for saving this service to the config. AWS services
        are not named the same in the config as they are in the rules processor, so
        having the ability to return a string like 'aws-s3' instead of 's3' is required

        Returns:
            [string] Service string used for looking up info in output configuration
        """
        return (self.__config_service__,
                self.__service__)[self.__config_service__ == NotImplemented]

    def format_output_config(self, config, props):
        """Add this descriptor to the list of descriptor this service
           If the service doesn't exist, a new entry is added to an empty list

        Args:
            config [dict]: Loaded configuration as a dictionary
            props [OrderedDict]: Contains various OutputProperty items
        Returns:
            [list<string>] List of descriptors for this service
        """
        return config.get(self.get_config_service(), []) + [props['descriptor'].value]

    def dispatch(self, descriptor, rule_name, alert):
        """Send alerts to the given service. This base class just
            logs an error if not implemented on the inheriting class

        Args:
            descriptor [string]: Service descriptor (ie: slack channel, pd integration)
            rule_name [string]: Name of the triggered rule
            alert [dict]: Alert relevant to the triggered rule
        """
        LOGGER.error('unable to send alert for service %s', self.__service__)