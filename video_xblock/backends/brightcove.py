# -*- coding: utf-8 -*-
"""
Brightcove Video player plugin.
"""

import base64
from datetime import datetime
import json
import httplib
import re
from xml.sax.saxutils import unescape

import requests
from xblock.fragment import Fragment

from video_xblock.backends.base import BaseVideoPlayer, BaseApiClient
from video_xblock.exceptions import ApiClientError, VideoXBlockException
from video_xblock.utils import ugettext as _


class BrightcoveApiClientError(ApiClientError):
    """
    Brightcove specific api client errors.
    """

    default_msg = _('Brightcove API error.')


class BrightcoveApiClient(BaseApiClient):
    """
    Low level Brightcove API client.

    Does all heavy lifting of sending https requests over the wire.
    Responsible for API credentials issuing and access_token refreshing.
    """

    def __init__(self, api_key, api_secret, token=None, account_id=None):
        """
        Initialize Brightcove API client.
        """
        if token and account_id:
            self.create_credentials(token, account_id)
        else:
            self.api_key = api_key
            self.api_secret = api_secret
        if api_key and api_secret:
            self.access_token = self._refresh_access_token()
        else:
            self.access_token = ''

    @staticmethod
    def create_credentials(token, account_id):
        """
        Get client credentials, given a client token and an account_id.

        Reference:
            https://docs.brightcove.com/en/video-cloud/oauth-api/guides/get-client-credentials.html
        """
        headers = {'Authorization': 'BC_TOKEN {}'.format(token)}
        data = {
            "type": "credential",
            "maximum_scope": [{
                "identity": {
                    "type": "video-cloud-account",
                    "account-id": int(account_id),
                },
                "operations": [
                    "video-cloud/video/all",
                    "video-cloud/ingest-profiles/profile/read",
                    "video-cloud/ingest-profiles/account/read",
                    "video-cloud/ingest-profiles/profile/write",
                    "video-cloud/ingest-profiles/account/write",
                ],
            }],
            "name": "Open edX Video XBlock"
        }
        url = 'https://oauth.brightcove.com/v4/client_credentials'
        response = requests.post(url, json=data, headers=headers)
        response_data = response.json()
        # New resource must have been created.
        if response.status_code == httplib.CREATED and response_data:
            client_secret = response_data.get('client_secret')
            client_id = response_data.get('client_id')
            error_message = ''
        else:
            # For dev purposes, response_data.get('error_description') may also be considered.
            error_message = "Authentication to Brightcove API failed: no client credentials have been retrieved.\n" \
                            "Please ensure you have provided an appropriate BC token, using Video API Token field."
            raise BrightcoveApiClientError(error_message)
        return client_secret, client_id, error_message

    def _refresh_access_token(self):
        """
        Request new access token to send with requests to Brightcove. Access Token expires every 5 minutes.
        """
        url = "https://oauth.brightcove.com/v3/access_token"
        params = {"grant_type": "client_credentials"}
        auth_string = base64.encodestring(
            '{}:{}'.format(self.api_key, self.api_secret)
        ).replace('\n', '')
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": "Basic " + auth_string
        }
        resp = requests.post(url, headers=headers, data=params)
        if resp.status_code == httplib.OK:
            result = resp.json()
            return result['access_token']

    def get(self, url, headers=None, can_retry=True):
        """
        Issue REST GET request to a given URL. Can throw ApiClientError or its subclass.

        Arguments:
            url (str): API url to fetch a resource from.
            headers (dict): Headers necessary as per API, e.g. authorization bearer to perform authorised requests.
            can_retry (bool): True if in a case of authentication error it can refresh access token and retry a call.
        Returns:
            Response in python native data format.
        """
        headers_ = {'Authorization': 'Bearer ' + str(self.access_token)}
        if headers is not None:
            headers_.update(headers)
        resp = requests.get(url, headers=headers_)
        if resp.status_code == httplib.OK:
            return resp.json()
        elif resp.status_code == httplib.UNAUTHORIZED and can_retry:
            self.access_token = self._refresh_access_token()
            return self.get(url, headers, can_retry=False)
        else:
            raise BrightcoveApiClientError

    def post(self, url, payload, headers=None, can_retry=True):
        """
        Issue REST POST request to a given URL. Can throw ApiClientError or its subclass.

        Arguments:
            url (str): API url to fetch a resource from.
            payload (dict): POST data.
            headers (dict): Headers necessary as per API, e.g. authorization bearer to perform authorised requests.
            can_retry (bool): True if in a case of authentication error it can refresh access token and retry a call.
        Returns:
            Response in Python native data format.
        """
        headers_ = {
            'Authorization': 'Bearer ' + self.access_token,
            'Content-type': 'application/json'
        }
        if headers is not None:
            headers_.update(headers)

        resp = requests.post(url, data=payload, headers=headers_)
        if resp.status_code in (httplib.OK, httplib.CREATED):
            return resp.json()
        elif resp.status_code == httplib.UNAUTHORIZED and can_retry:
            self.access_token = self._refresh_access_token()
            return self.post(url, payload, headers, can_retry=False)
        else:
            raise BrightcoveApiClientError


class BrightcoveHlsMixin(object):
    """
    Encapsulate data and methods used for HLS specific features.

    These features are:
    1. Video playback autoquality. i.e. adjusting video bitrate depending on client's bandwidth.
    2. Video content encryption using short-living keys.
    """

    DI_PROFILES = {
        'autoquality': {
            'name': 'Open edX Video XBlock HLS ingest profile',
            'short_name': 'autoquality',
            'path': '../static/json/brightcove-ingest-profile-hls.tmpl.json',
            'description': (
                'This profile is used by Open edX Video XBlock to enable auto-quality feature. '
                'Uploaded {:%Y-%m-%d %H:%M}'.format(datetime.now())
            )
        },
        'encryption': {
            'name': 'Open edX Video XBlock HLS with encryption ingest profile',
            'short_name': 'encryption',
            'path': '../static/json/brightcove-ingest-profile-hlse.tmpl.json',
            'description': (
                'This profile is used by Open edX Video XBlock to enable video content protection. '
                'Uploaded {:%Y-%m-%d %H:%M}'.format(datetime.now())
            )
        }
    }

    def ensure_ingest_profiles(self, account_id):
        """
        Check if custom HLS-enabled ingest profiles have been uploaded to the given Brightcove `account_id`.

        If not, upload these profiles.
        """
        existing_profiles = self.get_ingest_profiles(account_id)
        existing_profiles_names = [_['name'] for _ in existing_profiles]
        if self.DI_PROFILES['autoquality']['name'] not in existing_profiles_names:
            self.upload_ingest_profile(account_id, self.DI_PROFILES['autoquality'])
        if self.DI_PROFILES['encryption']['name'] not in existing_profiles_names:
            self.upload_ingest_profile(account_id, self.DI_PROFILES['encryption'])

    def get_ingest_profiles(self, account_id):
        """
        Get all Ingest Profiles available for a given `account_id`.

        Reference:
            https://docs.brightcove.com/en/video-cloud/ingest-profiles-api/getting-started/api-overview.html
        """
        url = 'https://ingestion.api.brightcove.com/v1/accounts/{}/profiles'.format(account_id)
        res = self.api_client.get(url)
        return res

    def upload_ingest_profile(self, account_id, ingest_profile):
        """
        Upload Ingest Profile to Brightcove using Brightcove's Ingest Profiles API.

        Reference:
            https://docs.brightcove.com/en/video-cloud/ingest-profiles-api/getting-started/api-overview.html
        """
        url = 'https://ingestion.api.brightcove.com/v1/accounts/{}/profiles'.format(account_id)
        profile = self.render_resource(
            ingest_profile['path'], name=ingest_profile['name'],
            account_id=account_id, description=ingest_profile['description']
        )
        resp = self.api_client.post(url, payload=json.dumps(json.loads(profile)))
        self.xblock.metadata[ingest_profile['short_name'] + '_profile_id'] = resp['id']
        return resp

    def submit_retranscode_job(self, account_id, video_id, profile_type):
        """
        Submit video for re-transcoding via Brightcove's Dynamic Ingestion API.

        profile_type:
            - default - re-transcode using default DI profile;
            - autoquality - re-transcode using HLS only profile;
            - encryption - re-transcode using HLS with encryption profile;
        """
        url = 'https://ingest.api.brightcove.com/v1/accounts/{account_id}/videos/{video_id}/ingest-requests'.format(
            account_id=account_id, video_id=video_id
        )
        retranscode_params = {
            'master': {
                'use_archived_master': True
            },
            # Notifications to be expected by callbacks
            # https://docs.brightcove.com/en/video-cloud/di-api/guides/notifications.html
            'callbacks': ['https://6da71d31.ngrok.io']
        }
        if profile_type != 'default':
            retranscode_params['profile'] = self.DI_PROFILES[profile_type]['name']
        res = self.api_client.post(url, json.dumps(retranscode_params))
        self.xblock.metadata['retranscode-status'] = (
            'ReTranscode request submitted {:%Y-%m-%d %H:%M} UTC using profile "{}". Job id: {}'.format(
                datetime.utcnow(), retranscode_params.get('profile', 'default'), res['id']))
        return res

    def get_video_renditions(self, account_id, video_id):
        """
        Return information about video renditions provided by Brightcove API.
        """
        url = 'https://cms.api.brightcove.com/v1/accounts/{account_id}/videos/{video_id}/assets/renditions'.format(
            account_id=account_id, video_id=video_id
        )
        res = self.api_client.get(url)
        return res

    def get_video_tech_info(self, account_id, video_id):
        """
        Return summary about given video.

        Returns:
            info (dict): Summary about given video. E.g.
                {
                  'renditions_count': <int>,
                  'auto_quality': 'on/off/partial',
                  'encryption': 'on/off/partial'
                }
        """
        renditions = self.get_video_renditions(account_id, video_id)
        info = {
            'auto_quality': 'off',
            'encryption': 'off',
            'renditions_count': len(renditions),
        }
        hls_renditions_count = sum(_['hls'] is not None for _ in renditions)
        encrypted_renditions_count = sum(_['hls']['encrypted'] for _ in renditions if _['hls'] is not None)

        if hls_renditions_count == len(renditions):
            info['auto_quality'] = 'on'
        elif hls_renditions_count > 0:
            info['auto_quality'] = 'partial'

        if encrypted_renditions_count == len(renditions):
            info['encryption'] = 'on'
        elif encrypted_renditions_count > 0:
            info['encryption'] = 'partial'

        return info


class BrightcovePlayer(BaseVideoPlayer, BrightcoveHlsMixin):
    """
    BrightcovePlayer is used for videos hosted on the Brightcove Video Cloud.
    """

    url_re = re.compile(r'https:\/\/studio.brightcove.com\/products(?:\/videocloud\/media)?\/videos\/(?P<media_id>\d+)')
    metadata_fields = ['access_token', 'client_id', 'client_secret', ]

    # Current api for requesting transcripts.
    # For example: https://cms.api.brightcove.com/v1/accounts/{account_id}/videos/{video_ID}
    # Docs on captions: https://docs.brightcove.com/en/video-cloud/cms-api/guides/webvtt.html
    # Docs on auth: https://docs.brightcove.com/en/video-cloud/oauth-api/getting-started/oauth-api-overview.html
    captions_api = {
        'url': 'https://cms.api.brightcove.com/v1/accounts/{account_id}/videos/{media_id}',
        'authorised_request_header': {
            'Authorization': 'Bearer {access_token}'
        },
        'response': {
            'language_code': 'srclang',  # no language_label translated in English may be fetched from API
            'subs': 'src'  # e.g. "http://learning-services-media.brightcove.com/captions/bc_smart_ja.vtt"
        }
    }

    # Stores default transcripts fetched from the captions API
    default_transcripts = []

    @property
    def basic_fields(self):
        """
        Tuple of VideoXBlock fields to display in Basic tab of edit modal window.

        Brightcove videos require Brightcove Account id.
        """
        return super(BrightcovePlayer, self).basic_fields + ['account_id']

    @property
    def advanced_fields(self):
        """
        Tuple of VideoXBlock fields to display in Basic tab of edit modal window.

        Brightcove videos require Brightcove Account id.
        """
        fields_list = ['player_id'] + super(BrightcovePlayer, self).advanced_fields
        # Add `token` field before `threeplaymedia_file_id`
        fields_list.insert(fields_list.index('threeplaymedia_file_id'), 'token')
        return fields_list

    fields_help = {
        'token': 'You can generate a BC token following the guide of '
                 '<a href="https://docs.brightcove.com/en/video-cloud/oauth-api/guides/get-client-credentials.html" '
                 'target="_blank">Brightcove</a>. Please ensure appropriate operations scope has been set '
                 'on the video platform, and a BC token is valid.'
    }

    def __init__(self, xblock):
        """
        Initialize Brightcove player class object.
        """
        super(BrightcovePlayer, self).__init__(xblock)
        self.api_key = xblock.metadata.get('client_id')
        self.api_secret = xblock.metadata.get('client_secret')
        self.api_client = BrightcoveApiClient(self.api_key, self.api_secret)

    def media_id(self, href):
        """
        Extract Platform's media id from the video url.
        """
        return self.url_re.match(href).group('media_id')

    def get_frag(self, **context):
        """
        Compose an XBlock fragment with video player to be rendered in student view.

        Brightcove backend is a special case and doesn't use vanilla Video.js player.
        Because of this it doesn't use `super.get_frag()`.
        """
        context['player_state'] = json.dumps(context['player_state'])

        frag = Fragment(
            self.render_template('brightcove.html', **context)
        )
        frag.add_css_url(
            'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css'
        )
        frag.add_javascript(
            self.render_resource('static/js/context.js', **context)
        )
        js_files = [
            'static/js/base.js',
            'static/js/videojs/toggle-button.js'
        ]
        js_files += [
            'static/js/videojs/videojs-tabindex.js',
            'static/js/videojs/videojs-event-plugin.js',
            'static/js/videojs/brightcove-videojs-init.js'
        ]

        for js_file in js_files:
            frag.add_javascript(self.resource_string(js_file))

        frag.add_css(
            self.resource_string('static/css/brightcove.css')
        )
        return frag

    def get_player_html(self, **context):
        """
        Add VideoJS plugins to the context and render player html using base class logic.
        """
        vjs_plugins = [
            self.resource_string(
                'static/vendor/js/videojs-offset.min.js'
            ),
            self.resource_string('static/js/videojs/videojs-speed-handler.js')
        ]
        if context.get('transcripts'):
            vjs_plugins += [
                self.resource_string(
                    'static/vendor/js/videojs-transcript.min.js'
                ),
                self.resource_string('static/js/videojs/videojs-transcript.js')
            ]
        context['vjs_plugins'] = vjs_plugins
        return super(BrightcovePlayer, self).get_player_html(**context)

    def dispatch(self, _request, suffix):
        """
        Brightcove dispatch method exposes different utility entry points.

        Entry point can either return info about video or Brightcove account
        or perform some action via Brightcove API.
        """
        if not self.api_key and self.api_secret:
            raise BrightcoveApiClientError(_('No API credentials provided'))

        if suffix == 'get_video_renditions':
            return self.get_video_renditions(
                self.xblock.account_id, self.media_id(self.xblock.href)
            )
        elif suffix == 'get_video_tech_info':
            return self.get_video_tech_info(
                self.xblock.account_id, self.media_id(self.xblock.href)
            )
        elif suffix == 'create_credentials':
            return self.create_credentials(
                self.xblock.token, self.xblock.account_id
            )
        elif suffix == 'get_ingest_profiles':
            return self.get_ingest_profiles(self.xblock.account_id)
        elif suffix == 'ensure_ingest_profiles':
            return self.ensure_ingest_profiles(self.xblock.account_id)

        elif suffix == 'submit_retranscode_default':
            return self.submit_retranscode_job(
                self.xblock.account_id, self.media_id(self.xblock.href), 'default'
            )
        elif suffix == 'submit_retranscode_autoquality':
            return self.submit_retranscode_job(
                self.xblock.account_id, self.media_id(self.xblock.href), 'autoquality'
            )
        elif suffix == 'submit_retranscode_encryption':
            return self.submit_retranscode_job(
                self.xblock.account_id, self.media_id(self.xblock.href), 'encryption'
            )
        elif suffix == 'retranscode-status':
            return self.xblock.metadata.get('retranscode-status')
        return {'success': False, 'message': 'Unknown method'}

    def can_show_settings(self):
        """
        Report to UI if it can show backend specific advanced settings.
        """
        can_show = bool(
            self.xblock.metadata.get('client_id') and
            self.xblock.metadata.get('client_secret')
        )
        return {'canShow': can_show}

    def authenticate_api(self, **kwargs):
        """
        Authenticate to a Brightcove API in order to perform authorized requests.

        Possible error messages:
            https://docs.brightcove.com/en/perform/oauth-api/reference/error-messages.html

        Arguments:
            kwargs (dict): Token and account_id key-value pairs as a platform-specific predefined client parameters,
            required to get credentials and access token.
        Returns:
            auth_data (dict): Tokens and credentials, necessary to perform authorised API requests.
            error_status_message (str): Error messages for the sake of verbosity.
        """
        token, account_id = kwargs.get('token'), kwargs.get('account_id')
        client_secret, client_id, error_message = BrightcoveApiClient.create_credentials(token, account_id)
        self.api_client.api_key = client_id
        self.api_client.api_secret = client_secret
        self.xblock.metadata['client_id'] = client_id
        self.xblock.metadata['client_secret'] = client_secret
        if error_message:
            return {}, error_message
        auth_data = {
            'client_secret': client_secret,
            'client_id': client_id,
        }
        return auth_data, error_message

    def get_default_transcripts(self, **kwargs):
        """
        Fetch transcripts list from a video platform.

        Arguments:
            kwargs (dict): Key-value pairs with account_id and video_id, fetched from video xblock,
                           and access_token, fetched from Brightcove API.
        Returns:
            default_transcripts (list): list of dicts of transcripts. Example:
                [
                    {
                        'lang': 'en',
                        'label': 'English',
                        'url': 'learning-services-media.brightcove.com/captions/bc_smart_ja.vtt'
                    },
                    # ...
                ]
            message (str): Message for a user with details on default transcripts fetching outcomes.
        """
        if not self.api_key and not self.api_secret:
            raise BrightcoveApiClientError(_('No API credentials provided'))

        video_id = kwargs.get('video_id')
        account_id = kwargs.get('account_id')
        url = self.captions_api['url'].format(account_id=account_id, media_id=video_id)
        message = ''
        self.default_transcripts = []
        # Fetch available transcripts' languages and urls if authentication succeeded.
        try:
            text = self.api_client.get(url)
        except BrightcoveApiClientError:
            message = 'No timed transcript may be fetched from a video platform.'
            return self.default_transcripts, message

        if text:
            captions_data = text.get('text_tracks')
            # Handle empty response (no subs uploaded on a platform)
            if not captions_data:
                message = 'For now, video platform doesn\'t have any timed transcript for this video.'
                return self.default_transcripts, message
            transcripts_data = [
                [el.get('src'), el.get('srclang')]
                for el in captions_data
            ]
            # Populate default_transcripts
            for transcript_url, lang_code in transcripts_data:
                lang_label = self.get_transcript_language_parameters(lang_code)[1]
                self.default_transcripts.append({
                    'lang': lang_code,
                    'label': lang_label,
                    'url': transcript_url,
                })
        else:
            try:
                # no way this code could be executed
                # TODO: refactor this code
                message = str(text[0].get('message'))
            except AttributeError:
                message = 'No timed transcript may be fetched from a video platform.'
        return self.default_transcripts, message

    def download_default_transcript(self, url=None, language_code=None):  # pylint: disable=unused-argument
        """
        Download default transcript from a video platform API in WebVVT format.

        Arguments:
            url (str): Transcript download url.
        Returns:
            sub (unicode): Transcripts formatted per WebVTT format https://w3c.github.io/webvtt/
        """
        if url is None:
            raise VideoXBlockException(_('`url` parameter is required.'))
        data = requests.get(url)
        text = data.content.decode('utf8')
        # To clean subs text from special symbols here, we need `unescape()` from xml.sax.saxutils
        # Reference: https://wiki.python.org/moin/EscapingHtml
        html_unescape_table = {
            "&amp;": "&",
            "&quot;": '"',
            "&amp;#39;": "'",
            "&apos;": "'",
            "&gt;": ">",
            "&lt;": "<"
        }
        unescaped_text = unescape(text, html_unescape_table)
        sub = unicode(unescaped_text)
        return sub
