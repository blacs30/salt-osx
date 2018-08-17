# -*- coding: utf-8 -*-
'''
Profiles Module
===============

Manage locally installed configuration profiles (.mobileconfig)

:maintainer:    Mosen <mosen@github.com>
:maturity:      new
:depends:       objc
:platform:      darwin
'''

import logging
import salt.utils
import salt.exceptions
import tempfile
import os
import plistlib
import uuid
import hashlib
import re
import binascii

log = logging.getLogger(__name__)

__virtualname__ = 'profile'


def __virtual__():
    return __virtualname__ if salt.utils.platform.is_darwin() else False


def _content_to_uuid(payload):
    '''
    Generate a UUID based upon the payload content

    :param payload:
    :return:
    '''
    str_payload = plistlib.writePlistToString(payload)
    hashobj = hashlib.md5(str_payload)

    identifier = re.sub(
        '([0-9a-f]{8})([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{12})',
        '\\1-\\2-\\3-\\4-\\5',
        binascii.hexlify(hashobj.digest()))

    return identifier


def _add_activedirectory_keys(payload):
    '''
    As per dayglojesus/managedmac, an excerpt from mobileconfig.rb:199

    The Advanced Active Directory profile contains flag keys which inform
    the installation process which configuration keys should actually be
    activated.

    http://support.apple.com/kb/HT5981?viewlocale=en_US&locale=en_US

    For example, if we wanted to change the default shell for AD accounts, we
    would actually need to define two keys: a configuration key and a flag key.

    <key>ADDefaultUserShell</key>
    <string>/bin/zsh</string>

    <key>ADDefaultUserShellFlag</key>
    <true/>

    If you fail to specify this second key (the activation or "flag" key), the
    configuration key will be ignored when the mobileconfig is processed.

    To avoid having to activate and deactivate the configuration keys, we
    pre-process the content array by overriding the transform_content method
    and shoehorn these flag keys into place dynamically, as required.
    :param payload:
    :return:
    '''
    needs_flag = ['ADAllowMultiDomainAuth',
                  'ADCreateMobileAccountAtLogin',
                  'ADDefaultUserShell',
                  'ADDomainAdminGroupList',
                  'ADForceHomeLocal',
                  'ADNamespace',
                  'ADPacketEncrypt',
                  'ADPacketSign',
                  'ADPreferredDCServer',
                  'ADRestrictDDNS',
                  'ADTrustChangePassIntervalDays',
                  'ADUseWindowsUNCPath',
                  'ADWarnUserBeforeCreatingMA']

    for k in payload.keys():
        if k in needs_flag:
            payload[needs_flag[k] + 'Flag'] = True


def _transform_payload(payload, identifier):
    '''
    Transform a payload by:
    - Calculating the UUID based upon a hash of the content.
    - Adding common keys required for every payload.
    - Adding required flags for the active directory payload

    :param payload:
    :param identifier:
    :return:
    '''
    if 'PayloadUUID' in payload:
        del payload['PayloadUUID']

    hashed_uuid = _content_to_uuid(payload)

    # No identifier supplied for the payload, so we generate one
    if 'PayloadIdentifier' not in payload:
        payload['PayloadIdentifier'] = "{0}.{1}".format(identifier, hashed_uuid)

    payload['PayloadUUID'] = hashed_uuid
    payload['PayloadEnabled'] = True
    payload['PayloadVersion'] = 1

    if payload['PayloadType'] == 'com.apple.DirectoryService.managed':
        _add_activedirectory_keys(payload)

    return payload


def _transform_content(content, identifier):
    '''
    As dayglojesus/managedmac notes:
    PayloadUUID for each Payload is modified MD5sum of the payload itself, minus some keys.
    We can use this to check whether or not the content has been modified. Even when the attributes cannot
    be compared (as with passwords, which are omitted).
    '''
    if not content:
        return list()

    transformed = [_transform_payload(payload, identifier) for payload in content]

    return transformed


def items():
    '''
    Retrieve all profiles in full

    CLI Example:

    .. code-block:: bash

        salt '*' profiles.items
    '''
    tmpdir = tempfile.mkdtemp('.profiles')
    tmpfile = os.path.join(tmpdir, 'profiles.plist')

    status = __salt__['cmd.retcode']('/usr/bin/profiles -P -o {}'.format(tmpfile))

    if not status == 0:
        raise salt.exceptions.CommandExecutionError(
            'Failed to read profiles or write to temporary file'
        )

    profiles = plistlib.readPlist(tmpfile)
    os.unlink(tmpfile)
    os.rmdir(tmpdir)

    return profiles


def exists(identifier):
    '''
    Determine whether a profile with the given identifier is installed.
    Returns True or False

    CLI Example:

    .. code-block:: bash

        salt '*' profiles.installed com.apple.mdm.hostname.local.ABCDEF
    '''
    profiles = __salt__['profile.items']()

    for domain, payload_content in profiles.items():
        for payload in payload_content:
            if payload['ProfileIdentifier'] == identifier:
                return True

    return False


def generate(identifier, profile_uuid=None, **kwargs):
    '''
    Generate a configuration profile.

    Intended to be used by other execution and state modules to prepare a profile for installation.
    Not really intended for CLI usage.

    As per the documentation, only the identifier and uuid are actually compulsory keys. It is possible to make
    a profile without anything else, however the profile will be downright useless.

    identifier
        The profile identifier, which is the primary key for identifying whether a profile is installed.

    profile_uuid
        Normally you would leave this blank, and the module will generate a UUID for you. However, if you specifically
        need to test with a fixed uuid, this can be set.

    Keyword arguments:

        description
            Description of the profile

        displayname
            The name of the profile shown to the user

        organization
            The organization issuing the profile

        content
            The payload content for the profile, as a hash

        removaldisallowed : False
            Whether removal of the profile will be allowed

        scope : System
            The scope of items to install, the default is system wide but may also be user.
            Note that only the System scope really makes sense in salt.

        removaldate
            The date on which the profile will be automatically removed.

        durationuntilremoval
            The number of seconds until profile is automatically removed, the smaller of this and removaldate will be
            used.

        consenttext : { "default": "message" }
            The warning/disclaimer shown when installing the profile interactively.
    '''
    if not profile_uuid:
        profile_uuid = uuid.uuid4()

    log.debug("Creating new profile with UUID: {}".format(str(profile_uuid)))

    VALID_PROPERTIES = ['description', 'displayname', 'organization', 'content', 'removaldisallowed', 'scope',
                        'removaldate', 'durationuntilremoval', 'consenttext']

    validkwargs = {k: v for k, v in kwargs.iteritems() if k in VALID_PROPERTIES}

    document = {'PayloadScope': 'System', 'PayloadUUID': str(profile_uuid), 'PayloadVersion': 1,
                'PayloadType': 'Configuration', 'PayloadIdentifier': identifier}

    for k, v in validkwargs.items():
        if k in ('__id__', 'fun', 'state', '__env__', '__sls__', 'order', 'watch', 'watch_in', 'require',
                 'require_in', 'prereq', 'prereq_in'):
            pass
        elif k == 'content':
            # As per managedmac for puppet, it's necessary to generate UUIDs for each payload based upon the content
            # in order to detect changes to the payload.
            # Transform a dict of { type: data } to { PayloadContent: data, }
            payload_content = _transform_content(kwargs['content'], identifier)
            document['PayloadContent'] = payload_content
        elif k == 'description':
            document['PayloadDescription'] = v
        elif k == 'displayname':
            document['PayloadDisplayName'] = v
        elif k == 'organization':
            document['PayloadOrganization'] = v
        elif k == 'removaldisallowed':
            document['PayloadRemovalDisallowed'] = (v is True)

    plist_content = plistlib.writePlistToString(document)
    return plist_content


def install(path):
    '''
    Install a configuration profile.

    path
        Full path to the configuration profile to install
    '''
    status = __salt__['cmd.retcode']('/usr/bin/profiles -I -F {}'.format(path))

    if not status == 0:
        raise salt.exceptions.CommandExecutionError(
            'Failed to install profile at path: {}'.format(path)
        )

    return True


def remove(identifier):
    '''
    Remove a configuration profile by its profile identifier

    identifier
        The ProfileIdentifier
    '''
    status = __salt__['cmd.retcode']('/usr/bin/profiles -R -p {}'.format(identifier))

    if not status == 0:
        raise salt.exceptions.CommandExecutionError(
            'Failed to remove profile with identifier: {}'.format(identifier)
        )

    return True
