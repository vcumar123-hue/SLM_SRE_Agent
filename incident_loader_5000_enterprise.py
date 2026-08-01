from __future__ import annotations

"""
Enterprise incident loader for AI Ops platform.
Generates 5,000 realistic canonical incidents across service desk, EUC,
identity, messaging, network, compute, database, cloud, kubernetes,
middleware, storage, security, application support, and change/release.

Usage:
    python incident_loader_5000_enterprise.py
    python incident_loader_5000_enterprise.py --count 5000 --output data/incidents.json
"""

import argparse
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import settings_portable as settings  # optional; used when available
except Exception:
    settings = None

SERVICE_SCOPE_CATALOG = {'Service Desk': {'domain': 'service_desk',
                  'weight': 0.15,
                  'team': 'Global Service Desk',
                  'services': ['Service Portal',
                               'VPN Client',
                               'SSO Portal',
                               'Outlook Desktop',
                               'Teams',
                               'Printer Services',
                               'Remote Access'],
                  'classifications': {'password_reset_loop': {'title': 'Users repeatedly prompted to reset password',
                                                              'symptoms': ['password reset completes but next login '
                                                                           'still fails',
                                                                           'password accepted on web but rejected on '
                                                                           'VPN',
                                                                           'account unlock request repeats every few '
                                                                           'hours'],
                                                              'causes': ['password sync delay between identity stores',
                                                                         'cached credentials on endpoint',
                                                                         'account lockout threshold triggered by stale '
                                                                         'mobile client'],
                                                              'checks': ['check account lockout events',
                                                                         'compare password last changed timestamp '
                                                                         'across identity sources',
                                                                         'validate cached credential prompts on '
                                                                         'affected endpoints'],
                                                              'actions': ['clear stale cached credentials',
                                                                          'force directory synchronization',
                                                                          'unlock account and confirm fresh sign-in'],
                                                              'automation_candidate': True},
                                      'mfa_enrollment_failure': {'title': 'MFA enrollment fails during setup',
                                                                 'symptoms': ['QR code scans but device does not '
                                                                              'register',
                                                                              'enrollment page loops back to start',
                                                                              'SMS enrollment code expires before '
                                                                              'validation'],
                                                                 'causes': ['conditional access enrollment exception '
                                                                            'missing',
                                                                            'time drift on mobile device',
                                                                            'identity provider enrollment backend '
                                                                            'latency'],
                                                                 'checks': ['review MFA enrollment logs',
                                                                            'check device clock skew',
                                                                            'validate conditional access enrollment '
                                                                            'policy'],
                                                                 'actions': ['reissue enrollment challenge',
                                                                             'temporarily bypass enrollment policy for '
                                                                             'validation',
                                                                             'sync device time and retry enrollment'],
                                                                 'automation_candidate': False},
                                      'vpn_client_failure': {'title': 'VPN client fails to connect',
                                                             'symptoms': ['user receives generic connection timeout',
                                                                          'VPN client disconnects after MFA',
                                                                          'only external remote users affected'],
                                                             'causes': ['expired machine certificate',
                                                                        'VPN gateway pool saturation',
                                                                        'client profile corruption'],
                                                             'checks': ['check VPN gateway health',
                                                                        'validate endpoint certificate validity',
                                                                        'review VPN client logs on affected machines'],
                                                             'actions': ['reissue endpoint certificate',
                                                                         'reset VPN client profile',
                                                                         'shift users to secondary gateway pool'],
                                                             'automation_candidate': True},
                                      'browser_sso_loop': {'title': 'Browser enters repeated SSO login loop',
                                                           'symptoms': ['login redirects between IdP and app '
                                                                        'repeatedly',
                                                                        'issue occurs only in one browser profile',
                                                                        'private browsing succeeds while regular '
                                                                        'session fails'],
                                                           'causes': ['corrupt browser cookies',
                                                                      'mismatched SAML relay state',
                                                                      'stale session token from legacy plugin'],
                                                           'checks': ['review browser developer redirects',
                                                                      'validate SAML response timestamps',
                                                                      'compare behavior in new browser profile'],
                                                           'actions': ['clear browser cookies for affected domains',
                                                                       'recreate application trust configuration',
                                                                       'disable conflicting browser extension'],
                                                           'automation_candidate': False},
                                      'shared_mailbox_access_issue': {'title': 'Unable to access shared mailbox',
                                                                      'symptoms': ['mailbox visible in Outlook but '
                                                                                   'folders do not open',
                                                                                   'web access works while desktop '
                                                                                   'access fails',
                                                                                   'only delegated users impacted'],
                                                                      'causes': ['delegation permission not replicated',
                                                                                 'autodiscover cache inconsistency',
                                                                                 'mailbox quota or corruption issue'],
                                                                      'checks': ['verify mailbox permissions',
                                                                                 'check autodiscover test results',
                                                                                 'review mailbox health diagnostics'],
                                                                      'actions': ['reapply delegation permissions',
                                                                                  'rebuild Outlook profile',
                                                                                  'run mailbox repair workflow'],
                                                                      'automation_candidate': False},
                                      'outlook_sync_issue': {'title': 'Outlook mailbox not syncing',
                                                             'symptoms': ['new mail arrives on mobile but not desktop',
                                                                          'send or receive stalls for multiple users',
                                                                          'folders show updating for long periods'],
                                                             'causes': ['profile OST corruption',
                                                                        'outlook add-in conflict',
                                                                        'mail service throttling after update'],
                                                             'checks': ['review Outlook connection status',
                                                                        'check add-ins loaded on affected devices',
                                                                        'validate mailbox service advisories'],
                                                             'actions': ['recreate Outlook profile',
                                                                         'disable conflicting add-ins',
                                                                         'move affected users to healthy service pool'],
                                                             'automation_candidate': False},
                                      'teams_audio_failure': {'title': 'Teams microphone or speaker not detected',
                                                              'symptoms': ['devices disappear only in Teams',
                                                                           'web version works but desktop app fails',
                                                                           'issue started after endpoint update'],
                                                              'causes': ['endpoint audio driver regression',
                                                                         'Teams device permission revoked',
                                                                         'device policy conflict in VDI profile'],
                                                              'checks': ['check endpoint audio devices',
                                                                         'review Teams client diagnostics',
                                                                         'validate privacy permissions for microphone '
                                                                         'access'],
                                                              'actions': ['restart Teams and media service',
                                                                          'roll back audio driver update',
                                                                          'reapply Teams device permissions'],
                                                              'automation_candidate': True},
                                      'printer_mapping_failure': {'title': 'Network printer mapping missing',
                                                                  'symptoms': ['printer disappears after user logon',
                                                                               'mapping works on one site but not '
                                                                               'another',
                                                                               'new users do not receive departmental '
                                                                               'printers'],
                                                                  'causes': ['group policy printer deployment failure',
                                                                             'print server queue name mismatch',
                                                                             'stale user profile mapping cache'],
                                                                  'checks': ['review group policy results',
                                                                             'check print server queue availability',
                                                                             'validate user OU mapping rules'],
                                                                  'actions': ['refresh printer group policy',
                                                                              'correct print queue mapping',
                                                                              'clear profile printer cache and '
                                                                              'relogin'],
                                                                  'automation_candidate': True},
                                      'software_install_failure': {'title': 'Standard software install fails',
                                                                   'symptoms': ['self-service install hangs at '
                                                                                'preparing',
                                                                                'package retries without completing',
                                                                                'issue occurs after packaging update'],
                                                                   'causes': ['endpoint management package detection '
                                                                              'rule incorrect',
                                                                              'installer prerequisite missing',
                                                                              'content distribution point replication '
                                                                              'lag'],
                                                                   'checks': ['review software deployment logs',
                                                                              'validate package detection rule',
                                                                              'check distribution point status'],
                                                                   'actions': ['redistribute application package',
                                                                               'fix detection rule',
                                                                               'push prerequisite package before '
                                                                               'retry'],
                                                                   'automation_candidate': True},
                                      'mail_quarantine_confusion': {'title': 'Users cannot locate quarantined email',
                                                                    'symptoms': ['expected message not delivered to '
                                                                                 'inbox',
                                                                                 'quarantine portal shows inconsistent '
                                                                                 'retention status',
                                                                                 'multiple business users report '
                                                                                 'missing external mail'],
                                                                    'causes': ['false positive mail security rule',
                                                                               'quarantine notification not delivered',
                                                                               'domain reputation threshold recently '
                                                                               'tightened'],
                                                                    'checks': ['review mail trace and quarantine '
                                                                               'events',
                                                                               'check anti-spam policy changes',
                                                                               'validate sender reputation status'],
                                                                    'actions': ['release safe messages from quarantine',
                                                                                'tune anti-spam rule',
                                                                                'whitelist validated sender domain'],
                                                                    'automation_candidate': False}}},
 'End User Computing': {'domain': 'end_user_computing',
                        'weight': 0.13,
                        'team': 'Workplace Services',
                        'services': ['Windows 11 Fleet',
                                     'VDI',
                                     'Citrix',
                                     'Endpoint Security Agent',
                                     'Docking Stations',
                                     'Wi-Fi',
                                     'BitLocker'],
                        'classifications': {'laptop_boot_failure': {'title': 'Laptop stuck during boot sequence',
                                                                    'symptoms': ['device remains on spinning dots for '
                                                                                 'several minutes',
                                                                                 'automatic repair starts after patch '
                                                                                 'window',
                                                                                 'safe mode still fails on select '
                                                                                 'devices'],
                                                                    'causes': ['corrupt boot files after interrupted '
                                                                               'patching',
                                                                               'endpoint disk encryption pre-boot '
                                                                               'issue',
                                                                               'firmware update applied '
                                                                               'inconsistently'],
                                                                    'checks': ['check last patch installed',
                                                                               'review endpoint boot diagnostics',
                                                                               'validate firmware and BIOS versions'],
                                                                    'actions': ['run startup repair workflow',
                                                                                'rollback last patch from recovery '
                                                                                'console',
                                                                                'apply approved BIOS remediation '
                                                                                'sequence'],
                                                                    'automation_candidate': False},
                                            'bitlocker_recovery_prompt': {'title': 'BitLocker recovery key requested',
                                                                          'symptoms': ['users prompted for recovery '
                                                                                       'key after reboot',
                                                                                       'devices recently moved to new '
                                                                                       'dock or motherboard replaced',
                                                                                       'multiple endpoints in one '
                                                                                       'hardware batch affected'],
                                                                          'causes': ['TPM measurement change',
                                                                                     'firmware settings drift',
                                                                                     'BitLocker protector metadata '
                                                                                     'mismatch'],
                                                                          'checks': ['check TPM health state',
                                                                                     'review recent hardware or BIOS '
                                                                                     'change records',
                                                                                     'validate key escrow in '
                                                                                     'directory'],
                                                                          'actions': ['retrieve recovery key and '
                                                                                      'unlock device',
                                                                                      're-seal TPM protectors',
                                                                                      'standardize BIOS settings and '
                                                                                      'resume protection'],
                                                                          'automation_candidate': False},
                                            'wifi_instability': {'title': 'Intermittent Wi-Fi disconnects',
                                                                 'symptoms': ['users drop from calls every 10 to 15 '
                                                                              'minutes',
                                                                              'issue mostly on one office floor',
                                                                              'wired connectivity remains healthy'],
                                                                 'causes': ['wireless channel interference',
                                                                            'roaming aggressiveness policy regression',
                                                                            'access point firmware mismatch'],
                                                                 'checks': ['review wireless controller events',
                                                                            'check client roaming metrics',
                                                                            'validate access point firmware parity'],
                                                                 'actions': ['retune channel plan',
                                                                             'adjust client roaming policy',
                                                                             'upgrade access point firmware on '
                                                                             'impacted zone'],
                                                                 'automation_candidate': True},
                                            'profile_corruption': {'title': 'User profile fails to load',
                                                                   'symptoms': ['temporary profile assigned at sign-in',
                                                                                'desktop icons and mapped drives '
                                                                                'missing',
                                                                                'issue follows a forced restart'],
                                                                   'causes': ['local profile registry corruption',
                                                                              'profile container locked',
                                                                              'insufficient profile disk space'],
                                                                   'checks': ['review user profile service logs',
                                                                              'check FSLogix or profile container '
                                                                              'status',
                                                                              'validate free space on profile storage'],
                                                                   'actions': ['repair or recreate local profile',
                                                                               'unlock profile container',
                                                                               'expand profile storage quota'],
                                                                   'automation_candidate': False},
                                            'citrix_launch_failure': {'title': 'Citrix desktop fails after login',
                                                                      'symptoms': ['workspace authenticates but '
                                                                                   'session never launches',
                                                                                   'launch spinner ends with protocol '
                                                                                   'error',
                                                                                   'affected users share same delivery '
                                                                                   'group'],
                                                                      'causes': ['VDA registration failure',
                                                                                 'Citrix profile issue',
                                                                                 'STA trust problem'],
                                                                      'checks': ['check Citrix delivery controller '
                                                                                 'health',
                                                                                 'review VDA registration status',
                                                                                 'validate storefront and STA '
                                                                                 'configuration'],
                                                                      'actions': ['restart unregistered VDA services',
                                                                                  'clear affected Citrix profiles',
                                                                                  'repair storefront trust '
                                                                                  'configuration'],
                                                                      'automation_candidate': True},
                                            'vdi_slowness': {'title': 'Virtual desktop response is slow',
                                                             'symptoms': ['mouse lag and delayed typing reported',
                                                                          'issue spikes during morning login wave',
                                                                          'storage latency higher than baseline'],
                                                             'causes': ['VDI host resource contention',
                                                                        'profile load delays',
                                                                        'backend storage latency'],
                                                             'checks': ['check host CPU and memory usage',
                                                                        'review login duration analytics',
                                                                        'validate storage latency and IOPS'],
                                                             'actions': ['rebalance sessions across hosts',
                                                                         'optimize profile exclusions',
                                                                         'move VDI workload to healthy storage tier'],
                                                             'automation_candidate': True},
                                            'endpoint_agent_unhealthy': {'title': 'Security endpoint agent unhealthy',
                                                                         'symptoms': ['agent stops reporting to '
                                                                                      'console',
                                                                                      'policy status stale for more '
                                                                                      'than 24 hours',
                                                                                      'user notices repeated local '
                                                                                      'notifications'],
                                                                         'causes': ['agent service stopped',
                                                                                    'backend certificate trust issue',
                                                                                    'tamper protection blocks '
                                                                                    'self-update'],
                                                                         'checks': ['check endpoint agent service '
                                                                                    'state',
                                                                                    'review agent logs and update '
                                                                                    'channel',
                                                                                    'validate trust chain for agent '
                                                                                    'backend'],
                                                                         'actions': ['restart endpoint agent service',
                                                                                     'repair agent installation',
                                                                                     'redeploy approved agent version'],
                                                                         'automation_candidate': True},
                                            'display_docking_issue': {'title': 'Docking station monitor not detected',
                                                                      'symptoms': ['second monitor not detected after '
                                                                                   'resume',
                                                                                   'issue appears after graphics '
                                                                                   'driver update',
                                                                                   'same dock works with other laptop '
                                                                                   'models'],
                                                                      'causes': ['display driver regression',
                                                                                 'USB-C firmware mismatch',
                                                                                 'dock model compatibility issue'],
                                                                      'checks': ['check display driver version',
                                                                                 'validate dock firmware',
                                                                                 'test external monitor path directly'],
                                                                      'actions': ['roll back display driver',
                                                                                  'update dock firmware',
                                                                                  'replace incompatible dock model'],
                                                                      'automation_candidate': False},
                                            'battery_health_alert': {'title': 'Laptop battery health degraded',
                                                                     'symptoms': ['device powers off under moderate '
                                                                                  'load',
                                                                                  'battery health warning shown in OEM '
                                                                                  'utility',
                                                                                  'charge capacity drops sharply '
                                                                                  'across same model line'],
                                                                     'causes': ['battery wear exceeding threshold',
                                                                                'firmware charging profile issue',
                                                                                'thermal management defect'],
                                                                     'checks': ['review battery wear metrics',
                                                                                'check BIOS charging settings',
                                                                                'compare thermal events on affected '
                                                                                'model'],
                                                                     'actions': ['schedule battery replacement',
                                                                                 'update BIOS and power firmware',
                                                                                 'adjust charging threshold policy'],
                                                                     'automation_candidate': False},
                                            'printer_driver_conflict': {'title': 'Printer driver conflict after update',
                                                                        'symptoms': ['print jobs disappear or spool '
                                                                                     'indefinitely',
                                                                                     'issue starts after workstation '
                                                                                     'update',
                                                                                     'legacy printer models most '
                                                                                     'affected'],
                                                                        'causes': ['print driver package conflict',
                                                                                   'spooler isolation disabled',
                                                                                   'unsigned legacy driver '
                                                                                   'reintroduced'],
                                                                        'checks': ['check print spooler events',
                                                                                   'validate driver package versions',
                                                                                   'review recent endpoint change '
                                                                                   'history'],
                                                                        'actions': ['remove conflicting driver '
                                                                                    'packages',
                                                                                    'restart spooler service',
                                                                                    'deploy universal print driver'],
                                                                        'automation_candidate': True}}},
 'Identity & Access': {'domain': 'identity_access',
                       'weight': 0.1,
                       'team': 'Identity Operations',
                       'services': ['Entra ID',
                                    'Active Directory',
                                    'Federation Services',
                                    'Privileged Access',
                                    'SSO Integrations'],
                       'classifications': {'sso_token_validation_failure': {'title': 'SSO token validation fails for '
                                                                                     'enterprise app',
                                                                            'symptoms': ['users authenticate '
                                                                                         'successfully but app rejects '
                                                                                         'session',
                                                                                         'issue starts after '
                                                                                         'certificate rollover',
                                                                                         'specific role-based users '
                                                                                         'impacted more often'],
                                                                            'causes': ['signing certificate mismatch',
                                                                                       'clock skew between IdP and SP',
                                                                                       'outdated JWKS cache on service '
                                                                                       'provider'],
                                                                            'checks': ['compare token issuer and '
                                                                                       'audience values',
                                                                                       'validate certificate '
                                                                                       'thumbprint in trust config',
                                                                                       'review JWKS cache refresh '
                                                                                       'timing'],
                                                                            'actions': ['refresh provider metadata',
                                                                                        'correct trust certificate '
                                                                                        'mapping',
                                                                                        'force JWKS cache reload'],
                                                                            'automation_candidate': True},
                                           'group_membership_delay': {'title': 'Group membership changes not reflected',
                                                                      'symptoms': ['new access still missing hours '
                                                                                   'after approval',
                                                                                   'cloud access updated but on-prem '
                                                                                   'access lags',
                                                                                   'membership shows correct in '
                                                                                   'request tool only'],
                                                                      'causes': ['directory sync backlog',
                                                                                 'approval workflow completed without '
                                                                                 'downstream provisioning',
                                                                                 'group nesting design delay'],
                                                                      'checks': ['check synchronization queue depth',
                                                                                 'review provisioning connector events',
                                                                                 'validate group nesting behavior'],
                                                                      'actions': ['rerun provisioning job',
                                                                                  'expedite sync cycle',
                                                                                  'flatten problematic nested group '
                                                                                  'assignment'],
                                                                      'automation_candidate': True},
                                           'account_lockout_storm': {'title': 'Account lockout storm across repeated '
                                                                              'login attempts',
                                                                     'symptoms': ['same users lock repeatedly after '
                                                                                  'unlock',
                                                                                  'mobile mail clients continue '
                                                                                  'prompting',
                                                                                  'service account failures appear in '
                                                                                  'parallel'],
                                                                     'causes': ['stale saved password on endpoint',
                                                                                'legacy scheduled task using expired '
                                                                                'credentials',
                                                                                'spray of failed auth from old device'],
                                                                     'checks': ['review lockout source telemetry',
                                                                                'check endpoint credential manager '
                                                                                'entries',
                                                                                'validate scheduled tasks and service '
                                                                                'accounts'],
                                                                     'actions': ['clear saved credentials',
                                                                                 'update scheduled task passwords',
                                                                                 'temporarily block offending source '
                                                                                 'until corrected'],
                                                                     'automation_candidate': True},
                                           'privileged_access_request_stuck': {'title': 'Privileged access request '
                                                                                        'remains pending or inactive',
                                                                               'symptoms': ['elevated session approval '
                                                                                            'completes but access not '
                                                                                            'granted',
                                                                                            'just-in-time access '
                                                                                            'expires immediately',
                                                                                            'PAM portal status '
                                                                                            'inconsistent'],
                                                                               'causes': ['PAM workflow callback '
                                                                                          'failure',
                                                                                          'role assignment policy '
                                                                                          'conflict',
                                                                                          'token propagation delay'],
                                                                               'checks': ['review PAM workflow logs',
                                                                                          'validate role assignment '
                                                                                          'schedule',
                                                                                          'check privileged token '
                                                                                          'issuance events'],
                                                                               'actions': ['replay PAM assignment '
                                                                                           'workflow',
                                                                                           'correct role activation '
                                                                                           'policy',
                                                                                           'reissue privileged token'],
                                                                               'automation_candidate': False},
                                           'service_account_expiry': {'title': 'Service account password expiry '
                                                                               'causing application impact',
                                                                      'symptoms': ['scheduled jobs begin failing '
                                                                                   'overnight',
                                                                                   'multiple services report '
                                                                                   'authentication errors',
                                                                                   'rotation reminders were not '
                                                                                   'acknowledged'],
                                                                      'causes': ['password expiry policy reached',
                                                                                 'rotation automation skipped '
                                                                                 'non-compliant target',
                                                                                 'manual exception not renewed'],
                                                                      'checks': ['check service account expiry dates',
                                                                                 'review password rotation logs',
                                                                                 'identify dependent applications '
                                                                                 'using the account'],
                                                                      'actions': ['rotate service account password',
                                                                                  'update dependent service '
                                                                                  'credentials',
                                                                                  'register account in rotation '
                                                                                  'automation'],
                                                                      'automation_candidate': True},
                                           'federation_metadata_sync_issue': {'title': 'Federation metadata not '
                                                                                       'synchronized',
                                                                              'symptoms': ['new trust settings visible '
                                                                                           'on IdP but not app',
                                                                                           'metadata refresh URL '
                                                                                           'accessible yet values '
                                                                                           'stale',
                                                                                           'issue follows cert '
                                                                                           'replacement'],
                                                                              'causes': ['federation metadata polling '
                                                                                         'failure',
                                                                                         'proxy cache serving stale '
                                                                                         'metadata',
                                                                                         'certificate chain validation '
                                                                                         'error'],
                                                                              'checks': ['check metadata fetch logs',
                                                                                         'validate metadata URL chain '
                                                                                         'and certificates',
                                                                                         'review proxy cache settings'],
                                                                              'actions': ['force metadata refresh',
                                                                                          'clear proxy cache',
                                                                                          'repair certificate chain '
                                                                                          'trust'],
                                                                              'automation_candidate': True},
                                           'conditional_access_policy_misfire': {'title': 'Conditional access blocks '
                                                                                          'expected users',
                                                                                 'symptoms': ['trusted users '
                                                                                              'challenged unexpectedly',
                                                                                              'managed devices denied '
                                                                                              'app entry',
                                                                                              'policy issue follows '
                                                                                              'recent exclusion '
                                                                                              'change'],
                                                                                 'causes': ['mis-scoped conditional '
                                                                                            'access policy',
                                                                                            'device compliance signal '
                                                                                            'lag',
                                                                                            'incorrect network '
                                                                                            'location tagging'],
                                                                                 'checks': ['review sign-in failure '
                                                                                            'reasons',
                                                                                            'validate policy includes '
                                                                                            'and excludes',
                                                                                            'check device compliance '
                                                                                            'reporting freshness'],
                                                                                 'actions': ['correct policy scoping',
                                                                                             're-sync compliance state',
                                                                                             'restore trusted network '
                                                                                             'location definition'],
                                                                                 'automation_candidate': False},
                                           'directory_replication_issue': {'title': 'Directory replication lag or '
                                                                                    'failure',
                                                                           'symptoms': ['password changes not '
                                                                                        'consistent across sites',
                                                                                        'new users missing from one '
                                                                                        'region',
                                                                                        'replication backlog increases '
                                                                                        'after maintenance'],
                                                                           'causes': ['site link replication issue',
                                                                                      'dc disk or journal pressure',
                                                                                      'DNS dependency for replication '
                                                                                      'broken'],
                                                                           'checks': ['check directory replication '
                                                                                      'status',
                                                                                      'review domain controller event '
                                                                                      'logs',
                                                                                      'validate replication DNS '
                                                                                      'records'],
                                                                           'actions': ['repair replication topology',
                                                                                       'restart affected replication '
                                                                                       'services',
                                                                                       'correct DNS records used by '
                                                                                       'directory'],
                                                                           'automation_candidate': True}}},
 'Messaging & Collaboration': {'domain': 'messaging_collaboration',
                               'weight': 0.08,
                               'team': 'Messaging Services',
                               'services': ['Exchange Online', 'Teams', 'SharePoint', 'OneDrive', 'Video Conferencing'],
                               'classifications': {'exchange_transport_delay': {'title': 'Email delivery delayed in '
                                                                                         'transport',
                                                                                'symptoms': ['messages queued for more '
                                                                                             'than fifteen minutes',
                                                                                             'external recipients '
                                                                                             'receive delayed delivery '
                                                                                             'reports',
                                                                                             'mail flow issue isolated '
                                                                                             'to one connector'],
                                                                                'causes': ['mail transport connector '
                                                                                           'backlog',
                                                                                           'smart host DNS issue',
                                                                                           'transport rule evaluation '
                                                                                           'latency'],
                                                                                'checks': ['review message trace and '
                                                                                           'queue length',
                                                                                           'check connector health',
                                                                                           'validate smart host DNS '
                                                                                           'resolution'],
                                                                                'actions': ['reroute through healthy '
                                                                                            'connector',
                                                                                            'clear transport backlog',
                                                                                            'adjust misfiring '
                                                                                            'transport rule'],
                                                                                'automation_candidate': True},
                                                   'teams_presence_incorrect': {'title': 'Teams presence status '
                                                                                         'inaccurate or stale',
                                                                                'symptoms': ['users appear offline '
                                                                                             'while actively in '
                                                                                             'meetings',
                                                                                             'presence updates delayed '
                                                                                             'across devices',
                                                                                             'issue spikes after '
                                                                                             'tenant update'],
                                                                                'causes': ['presence service sync '
                                                                                           'issue',
                                                                                           'teams client cache '
                                                                                           'corruption',
                                                                                           'hybrid calendar '
                                                                                           'integration lag'],
                                                                                'checks': ['check Teams service health',
                                                                                           'review client cache '
                                                                                           'behavior',
                                                                                           'validate calendar '
                                                                                           'integration state'],
                                                                                'actions': ['clear Teams client cache',
                                                                                            'restart presence sync '
                                                                                            'components',
                                                                                            'repair calendar '
                                                                                            'integration'],
                                                                                'automation_candidate': True},
                                                   'sharepoint_permission_drift': {'title': 'SharePoint or OneDrive '
                                                                                            'permission drift',
                                                                                   'symptoms': ['users lose access to '
                                                                                                'site unexpectedly',
                                                                                                'access granted '
                                                                                                'through group but not '
                                                                                                'inherited',
                                                                                                'issue follows site '
                                                                                                'restructuring'],
                                                                                   'causes': ['broken inheritance '
                                                                                              'after site changes',
                                                                                              'group sync delay',
                                                                                              'manual permission '
                                                                                              'override'],
                                                                                   'checks': ['review site permission '
                                                                                              'inheritance',
                                                                                              'check group membership '
                                                                                              'propagation',
                                                                                              'compare item versus '
                                                                                              'site permissions'],
                                                                                   'actions': ['restore inherited '
                                                                                               'permissions',
                                                                                               'reapply group-based '
                                                                                               'access model',
                                                                                               'remove conflicting '
                                                                                               'manual ACLs'],
                                                                                   'automation_candidate': False},
                                                   'meeting_join_failure': {'title': 'Users unable to join meeting or '
                                                                                     'share content',
                                                                            'symptoms': ['join button spins then exits',
                                                                                         'content share fails while '
                                                                                         'audio works',
                                                                                         'conference room systems '
                                                                                         'impacted too'],
                                                                            'causes': ['meeting service media relay '
                                                                                       'issue',
                                                                                       'client version regression',
                                                                                       'network ports blocked for '
                                                                                       'media path'],
                                                                            'checks': ['review Teams call analytics',
                                                                                       'check conference room device '
                                                                                       'logs',
                                                                                       'validate required media ports '
                                                                                       'and paths'],
                                                                            'actions': ['upgrade affected client '
                                                                                        'version',
                                                                                        'restart room device service',
                                                                                        'open blocked media path on '
                                                                                        'firewall'],
                                                                            'automation_candidate': False},
                                                   'onedrive_sync_backlog': {'title': 'OneDrive files remain pending '
                                                                                      'or out of sync',
                                                                             'symptoms': ['known folders stop syncing',
                                                                                          'sync client reports '
                                                                                          'processing changes for '
                                                                                          'hours',
                                                                                          'large files or many renames '
                                                                                          'preceded issue'],
                                                                             'causes': ['sync client database '
                                                                                        'corruption',
                                                                                        'path length or invalid '
                                                                                        'characters',
                                                                                        'tenant throttling due to '
                                                                                        'spike of changes'],
                                                                             'checks': ['check OneDrive sync '
                                                                                        'diagnostics',
                                                                                        'review recent file rename '
                                                                                        'patterns',
                                                                                        'validate known folder move '
                                                                                        'policy'],
                                                                             'actions': ['reset OneDrive sync client',
                                                                                         'correct invalid paths',
                                                                                         'stagger bulk sync changes'],
                                                                             'automation_candidate': True},
                                                   'calendar_free_busy_issue': {'title': 'Calendar free or busy data '
                                                                                         'missing',
                                                                                'symptoms': ['users cannot view '
                                                                                             'availability of certain '
                                                                                             'mailboxes',
                                                                                             'hybrid users affected '
                                                                                             'more often',
                                                                                             'issue appears after '
                                                                                             'mailbox migration'],
                                                                                'causes': ['availability service '
                                                                                           'configuration issue',
                                                                                           'autodiscover mismatch',
                                                                                           'organization relationship '
                                                                                           'stale'],
                                                                                'checks': ['test autodiscover and free '
                                                                                           'busy lookups',
                                                                                           'review availability '
                                                                                           'service logs',
                                                                                           'validate organization '
                                                                                           'relationship '
                                                                                           'configuration'],
                                                                                'actions': ['repair autodiscover '
                                                                                            'settings',
                                                                                            'refresh organization '
                                                                                            'relationship',
                                                                                            're-run mailbox migration '
                                                                                            'finalization'],
                                                                                'automation_candidate': False}}},
 'Network': {'domain': 'network',
             'weight': 0.08,
             'team': 'Network Operations',
             'services': ['LAN', 'WAN', 'DNS', 'Load Balancers', 'Firewall', 'VPN', 'Proxy'],
             'classifications': {'dns_resolution_failure': {'title': 'Internal DNS names fail to resolve',
                                                            'symptoms': ['applications fail only when using hostname',
                                                                         'external DNS still resolves',
                                                                         'pods or servers query stale response'],
                                                            'causes': ['stale DNS record replication',
                                                                       'conditional forwarder issue',
                                                                       'dns cache not refreshed after change'],
                                                            'checks': ['check DNS server health and zone replication',
                                                                       'compare responses from multiple resolvers',
                                                                       'review recent DNS record changes'],
                                                            'actions': ['force zone replication',
                                                                        'flush resolver caches',
                                                                        'correct conditional forwarder configuration'],
                                                            'automation_candidate': True},
                                 'packet_loss_branch': {'title': 'Packet loss from branch office',
                                                        'symptoms': ['voice quality degrades from one site',
                                                                     'application disconnects increase during business '
                                                                     'hours',
                                                                     'monitoring shows interface drops on branch edge'],
                                                        'causes': ['wan circuit congestion',
                                                                   'edge interface errors',
                                                                   'provider flap on access loop'],
                                                        'checks': ['check interface counters',
                                                                   'review circuit utilization trend',
                                                                   'compare provider handoff metrics'],
                                                        'actions': ['reroute traffic to backup path',
                                                                    'raise provider incident with evidence',
                                                                    'replace failing edge interface'],
                                                        'automation_candidate': True},
                                 'vpn_latency_spike': {'title': 'VPN latency exceeds threshold',
                                                       'symptoms': ['remote users report slow applications',
                                                                    'latency higher on one gateway pool',
                                                                    'performance degrades after peak login window'],
                                                       'causes': ['gateway CPU pressure',
                                                                  'tunnel path congestion',
                                                                  'suboptimal gateway selection'],
                                                       'checks': ['check VPN gateway utilization',
                                                                  'review tunnel performance by region',
                                                                  'validate client gateway assignment'],
                                                       'actions': ['shift users to lower-utilized gateway',
                                                                   'scale gateway pool',
                                                                   'optimize tunnel routing policy'],
                                                       'automation_candidate': True},
                                 'firewall_rule_block': {'title': 'Firewall rule blocks business traffic',
                                                         'symptoms': ['new application deployment cannot reach backend',
                                                                      'only one zone pair affected',
                                                                      'traffic denied after policy cleanup window'],
                                                         'causes': ['rule ordering conflict',
                                                                    'object group not updated',
                                                                    'policy pushed without dependent service '
                                                                    'exception'],
                                                         'checks': ['review firewall deny logs',
                                                                    'check policy diff from last change',
                                                                    'validate object group membership'],
                                                         'actions': ['insert exception rule in correct order',
                                                                     'update object group members',
                                                                     'rollback faulty policy package'],
                                                         'automation_candidate': False},
                                 'load_balancer_node_issue': {'title': 'Single load balancer node causing latency',
                                                              'symptoms': ['latency spikes only on one backend path',
                                                                           'health checks pass but user response time '
                                                                           'is poor',
                                                                           'issue disappears after pool rebalance'],
                                                              'causes': ['one node with partial resource exhaustion',
                                                                         'persistence table imbalance',
                                                                         'ssl offload problem on specific member'],
                                                              'checks': ['compare node-level response metrics',
                                                                         'review persistence distribution',
                                                                         'check ssl handshake failures by node'],
                                                              'actions': ['drain affected node from pool',
                                                                          'clear persistence table imbalance',
                                                                          'restart load balancer worker on impacted '
                                                                          'node'],
                                                              'automation_candidate': True},
                                 'proxy_auth_failure': {'title': 'Proxy authentication or PAC routing failure',
                                                        'symptoms': ['web browsing breaks for managed users',
                                                                     'cloud apps reachable on mobile hotspot only',
                                                                     'PAC file download intermittently fails'],
                                                        'causes': ['proxy auth backend latency',
                                                                   'PAC file publication issue',
                                                                   'certificate inspection mismatch'],
                                                        'checks': ['check proxy auth health',
                                                                   'validate PAC file distribution',
                                                                   'review ssl inspection exceptions'],
                                                        'actions': ['fail over proxy auth backend',
                                                                    'republish PAC file',
                                                                    'adjust inspection bypass for trusted SaaS '
                                                                    'endpoints'],
                                                        'automation_candidate': True},
                                 'wireless_controller_issue': {'title': 'Wireless controller instability impacting '
                                                                        'office users',
                                                               'symptoms': ['multiple floors report dropped sessions',
                                                                            'APs rejoin controller repeatedly',
                                                                            'controller cpu spikes after config push'],
                                                               'causes': ['controller software defect',
                                                                          'bad config rollout',
                                                                          'AP certificate mismatch'],
                                                               'checks': ['review controller logs and AP join events',
                                                                          'check last config deployment',
                                                                          'validate controller resource utilization'],
                                                               'actions': ['rollback controller config',
                                                                           'restart failed controller instance',
                                                                           'reissue AP join certificates'],
                                                               'automation_candidate': True}}},
 'Servers & Compute': {'domain': 'compute',
                       'weight': 0.08,
                       'team': 'Compute Operations',
                       'services': ['Windows Servers', 'Linux Servers', 'VMware', 'Hyper-V', 'Batch Services'],
                       'classifications': {'cpu_saturation': {'title': 'CPU sustained above threshold on server fleet',
                                                              'symptoms': ['application latency increases with high '
                                                                           'run queue',
                                                                           'top process aligns with recent deployment',
                                                                           'virtualized hosts show no contention '
                                                                           'elsewhere'],
                                                              'causes': ['runaway application process',
                                                                         'scheduled batch overlap',
                                                                         'insufficient compute sizing after growth'],
                                                              'checks': ['check top CPU consumers',
                                                                         'review scheduler windows',
                                                                         'compare baseline utilization against last '
                                                                         'month'],
                                                              'actions': ['restart runaway process',
                                                                          'stagger batch jobs',
                                                                          'scale VM resources or rebalance workload'],
                                                              'automation_candidate': True},
                                           'memory_pressure': {'title': 'Memory exhaustion causing swap or paging',
                                                               'symptoms': ['response times degrade before service '
                                                                            'restart',
                                                                            'swap usage persists after traffic '
                                                                            'normalizes',
                                                                            'same service leaks memory after patch'],
                                                               'causes': ['application memory leak',
                                                                          'cache growth without eviction',
                                                                          'resource limits too low for workload'],
                                                               'checks': ['review memory utilization trend',
                                                                          'check process heap growth',
                                                                          'validate service cache configuration'],
                                                               'actions': ['restart leaking service',
                                                                           'increase memory allocation',
                                                                           'apply patch with memory leak fix'],
                                                               'automation_candidate': True},
                                           'disk_full': {'title': 'Server disk usage above threshold',
                                                         'symptoms': ['application writes fail on one filesystem',
                                                                      'log partition grows rapidly overnight',
                                                                      'cleanup task did not run'],
                                                         'causes': ['log rotation failure',
                                                                    'unexpected dump files',
                                                                    'backup staging data not purged'],
                                                         'checks': ['check largest directories',
                                                                    'review log rotation status',
                                                                    'validate cleanup job history'],
                                                         'actions': ['purge transient files',
                                                                     'restart or fix log rotation',
                                                                     'expand filesystem where approved'],
                                                         'automation_candidate': True},
                                           'service_crash_loop': {'title': 'Service repeatedly crashes and restarts',
                                                                  'symptoms': ['service recovers briefly then exits '
                                                                               'again',
                                                                               'crash follows new config deployment',
                                                                               'health checks flap for affected nodes'],
                                                                  'causes': ['invalid configuration parameter',
                                                                             'dependency timeout at startup',
                                                                             'binary incompatibility after patch'],
                                                                  'checks': ['review service crash logs',
                                                                             'compare config checksum to baseline',
                                                                             'check dependency startup sequence'],
                                                                  'actions': ['rollback bad config',
                                                                              'delay service start until dependency '
                                                                              'healthy',
                                                                              'restore last known good binary'],
                                                                  'automation_candidate': True},
                                           'patch_failure': {'title': 'OS patch deployment failed on server batch',
                                                             'symptoms': ['servers remain pending reboot for extended '
                                                                          'period',
                                                                          'patch job marks compliance failed',
                                                                          'subset of images consistently fail one '
                                                                          'update'],
                                                             'causes': ['package dependency failure',
                                                                        'reboot orchestration issue',
                                                                        'stale update cache'],
                                                             'checks': ['check patch job results',
                                                                        'review update logs',
                                                                        'validate pre-requisites and free space'],
                                                             'actions': ['clear update cache',
                                                                         'retry patch with pre-checks',
                                                                         'exclude problematic update until validated'],
                                                             'automation_candidate': True},
                                           'ntp_drift': {'title': 'Server time drift impacting authentication and jobs',
                                                         'symptoms': ['kerberos or token validation errors appear',
                                                                      'scheduled jobs start at unexpected times',
                                                                      'drift isolated to one virtualization cluster'],
                                                         'causes': ['NTP source unreachable',
                                                                    'hypervisor time sync conflict',
                                                                    'manual time adjustment left enabled'],
                                                         'checks': ['check ntp peers and offset',
                                                                    'review hypervisor tools time sync settings',
                                                                    'compare server time across cluster'],
                                                         'actions': ['restart time synchronization service',
                                                                     'disable conflicting hypervisor time sync',
                                                                     'point servers to healthy NTP source'],
                                                         'automation_candidate': True},
                                           'filesystem_mount_failure': {'title': 'Filesystem or mount dependency '
                                                                                 'unavailable',
                                                                        'symptoms': ['application starts before mount '
                                                                                     'ready',
                                                                                     'system boot waits for missing '
                                                                                     'network storage',
                                                                                     'only one server pool affected'],
                                                                        'causes': ['stale mount target',
                                                                                   'credential or kerberos issue for '
                                                                                   'network mount',
                                                                                   'service ordering problem'],
                                                                        'checks': ['check mount status and errors',
                                                                                   'validate storage endpoint '
                                                                                   'reachability',
                                                                                   'review boot dependency ordering'],
                                                                        'actions': ['remount target after dependency '
                                                                                    'recovery',
                                                                                    'correct mount credentials',
                                                                                    'fix service ordering with mount '
                                                                                    'dependency'],
                                                                        'automation_candidate': False}}},
 'Database': {'domain': 'database',
              'weight': 0.06,
              'team': 'Database Operations',
              'services': ['SQL Server', 'Oracle', 'PostgreSQL', 'MySQL', 'DB Middleware'],
              'classifications': {'connection_pool_exhaustion': {'title': 'Database connections exhausted under burst '
                                                                          'traffic',
                                                                 'symptoms': ['application threads wait on connection '
                                                                              'checkout',
                                                                              'database CPU remains normal while login '
                                                                              'attempts spike',
                                                                              'incidents align with traffic burst or '
                                                                              'connection leak'],
                                                                 'causes': ['application connection leak',
                                                                            'pool size too low',
                                                                            'long-running transactions hold sessions'],
                                                                 'checks': ['check active sessions by application',
                                                                            'review pool utilization and timeouts',
                                                                            'identify long-running transactions'],
                                                                 'actions': ['restart leaking app instances',
                                                                             'increase pool size within safe limits',
                                                                             'terminate blocking orphan sessions'],
                                                                 'automation_candidate': True},
                                  'replication_lag': {'title': 'Replication lag increasing on replica set',
                                                      'symptoms': ['read replica data stale beyond SLA',
                                                                   'lag spikes after bulk write jobs',
                                                                   'failover not safe while lag remains high'],
                                                      'causes': ['network throughput bottleneck',
                                                                 'replica IO saturation',
                                                                 'large transaction backlog'],
                                                      'checks': ['check replication queue depth',
                                                                 'review replica disk latency',
                                                                 'identify bulk write sources'],
                                                      'actions': ['throttle bulk job',
                                                                  'move read traffic off lagging replica',
                                                                  'scale replica storage throughput'],
                                                      'automation_candidate': False},
                                  'slow_query_spike': {'title': 'Slow query count increased after deployment',
                                                       'symptoms': ['top queries regress in execution time',
                                                                    'same endpoint drives both app and DB alerts',
                                                                    'plan cache changed around release'],
                                                       'causes': ['query plan regression',
                                                                  'missing index after schema change',
                                                                  'parameter sniffing issue'],
                                                       'checks': ['review top slow queries',
                                                                  'compare current versus previous execution plans',
                                                                  'check index usage and missing index hints'],
                                                       'actions': ['force known good plan',
                                                                   'create or rebuild required index',
                                                                   'apply query hint or parameterization fix'],
                                                       'automation_candidate': False},
                                  'deadlock_detected': {'title': 'Database deadlocks observed',
                                                        'symptoms': ['users see transaction retries or failures',
                                                                     'deadlock graphs show same stored procedure pair',
                                                                     'issue peaks at month-end batch windows'],
                                                        'causes': ['transaction order inconsistency',
                                                                   'new index changing lock behavior',
                                                                   'long-running update colliding with reads'],
                                                        'checks': ['review deadlock graphs',
                                                                   'check recent schema changes',
                                                                   'analyze transaction order in application code'],
                                                        'actions': ['reschedule conflicting jobs',
                                                                    'add index to reduce lock footprint',
                                                                    'update transaction ordering or retry logic'],
                                                        'automation_candidate': False},
                                  'tempdb_growth_issue': {'title': 'TempDB or temp tablespace growth impacting '
                                                                   'performance',
                                                          'symptoms': ['sort or temp object spills increase sharply',
                                                                       'disk pressure rises on temp volume',
                                                                       'reporting jobs coincide with issue'],
                                                          'causes': ['query spill due to poor plan',
                                                                     'insufficient tempdb files',
                                                                     'report burst creating large temp objects'],
                                                          'checks': ['check tempdb usage by session',
                                                                     'review query spill events',
                                                                     'validate tempdb file configuration'],
                                                          'actions': ['add or resize tempdb files',
                                                                      'optimize spilling query',
                                                                      'move reporting workload off peak window'],
                                                          'automation_candidate': False},
                                  'backup_job_failure': {'title': 'Database backup job failed or exceeded window',
                                                         'symptoms': ['nightly backup incomplete by morning',
                                                                      'backup chain warning raised',
                                                                      'storage target slower than baseline'],
                                                         'causes': ['backup target capacity issue',
                                                                    'network throughput drop',
                                                                    'log growth exceeded backup window'],
                                                         'checks': ['check backup job history',
                                                                    'validate backup storage capacity',
                                                                    'review backup throughput trend'],
                                                         'actions': ['rerun failed backup',
                                                                     'free target space or switch repository',
                                                                     'split oversized backup job into tiers'],
                                                         'automation_candidate': True}}},
 'Cloud Platform': {'domain': 'cloud',
                    'weight': 0.06,
                    'team': 'Cloud Operations',
                    'services': ['AWS', 'Azure', 'GCP', 'Terraform', 'Cloud Networking'],
                    'classifications': {'autoscaling_failure': {'title': 'Autoscaling policy not adding nodes',
                                                                'symptoms': ['cpu or queue depth high while instance '
                                                                             'count flat',
                                                                             'scaling alarm fires but no capacity '
                                                                             'added',
                                                                             'cooldown shows active longer than '
                                                                             'expected'],
                                                                'causes': ['autoscaling policy misconfiguration',
                                                                           'quota or capacity limit reached',
                                                                           'launch template invalid after image '
                                                                           'update'],
                                                                'checks': ['check autoscaling activity history',
                                                                           'review quota and capacity errors',
                                                                           'validate launch template and health '
                                                                           'checks'],
                                                                'actions': ['fix autoscaling policy threshold',
                                                                            'raise or free quota',
                                                                            'roll back to last healthy launch '
                                                                            'template'],
                                                                'automation_candidate': True},
                                        'quota_exceeded': {'title': 'Cloud quota exhausted for service deployment',
                                                           'symptoms': ['new instances or disks fail to provision',
                                                                        'IaC runs fail only in one region',
                                                                        'quota warnings ignored before release'],
                                                           'causes': ['regional quota limit reached',
                                                                      'orphaned resources consuming quota',
                                                                      'unexpected growth in scale set'],
                                                           'checks': ['check regional quota usage',
                                                                      'identify orphaned compute and storage',
                                                                      'review recent IaC changes increasing footprint'],
                                                           'actions': ['delete orphaned resources',
                                                                       'request quota increase',
                                                                       'shift deployment to alternate region or '
                                                                       'subscription'],
                                                           'automation_candidate': True},
                                        'disk_detach_issue': {'title': 'Persistent disk detach or attach failure',
                                                              'symptoms': ['node replacement hangs on volume detach',
                                                                           'stateful workload stays pending',
                                                                           'cloud events show attachment timeout'],
                                                              'causes': ['storage API transient failure',
                                                                         'stale attachment lock',
                                                                         'node instance not fully terminated'],
                                                              'checks': ['check cloud storage event logs',
                                                                         'review node lifecycle state',
                                                                         'validate volume attachment status'],
                                                              'actions': ['force detach stale volume',
                                                                          'terminate hung instance',
                                                                          'reattach volume to healthy node'],
                                                              'automation_candidate': True},
                                        'vm_boot_issue': {'title': 'Cloud VM fails to boot after image change',
                                                          'symptoms': ['serial console shows boot loader errors',
                                                                       'same image works in test but not prod',
                                                                       'boot diagnostics missing expected agent '
                                                                       'heartbeat'],
                                                          'causes': ['image customization defect',
                                                                     'boot disk corruption',
                                                                     'cloud-init or agent failure'],
                                                          'checks': ['check boot diagnostics and serial console',
                                                                     'compare image version to last known good',
                                                                     'validate cloud-init or startup scripts'],
                                                          'actions': ['redeploy from last healthy image',
                                                                      'repair boot disk snapshot',
                                                                      'fix startup script or cloud-init configuration'],
                                                          'automation_candidate': False},
                                        'iam_policy_block': {'title': 'IAM policy blocks workload access to cloud '
                                                                      'service',
                                                             'symptoms': ['service loses access to secret store or '
                                                                          'bucket',
                                                                          'workload identity token issued but '
                                                                          'authorization denied',
                                                                          'issue follows least-privilege cleanup'],
                                                             'causes': ['missing role assignment',
                                                                        'service principal rotation incomplete',
                                                                        'policy condition too restrictive'],
                                                             'checks': ['review cloud audit logs',
                                                                        'validate workload identity and role '
                                                                        'assignment',
                                                                        'compare policy diff with prior version'],
                                                             'actions': ['restore required role assignment',
                                                                         'complete principal credential rotation',
                                                                         'correct restrictive policy condition'],
                                                             'automation_candidate': False},
                                        'terraform_state_lock_issue': {'title': 'Infrastructure as code run blocked by '
                                                                                'state lock',
                                                                       'symptoms': ['pipeline waits on remote state '
                                                                                    'indefinitely',
                                                                                    'lock remains after previous job '
                                                                                    'failure',
                                                                                    'manual deploy attempts also '
                                                                                    'blocked'],
                                                                       'causes': ['stale state lock after interrupted '
                                                                                  'run',
                                                                                  'backend storage latency',
                                                                                  'duplicate pipeline run collision'],
                                                                       'checks': ['check remote state backend health',
                                                                                  'review last successful IaC run',
                                                                                  'validate lock owner metadata'],
                                                                       'actions': ['safely release stale state lock',
                                                                                   'serialize duplicate pipeline runs',
                                                                                   'repair backend storage '
                                                                                   'performance'],
                                                                       'automation_candidate': True}}},
 'Kubernetes & Containers': {'domain': 'kubernetes',
                             'weight': 0.05,
                             'team': 'Container Platform',
                             'services': ['Kubernetes', 'OpenShift', 'Ingress', 'Container Registry', 'Service Mesh'],
                             'classifications': {'pod_crash_loop': {'title': 'Pods in CrashLoopBackOff',
                                                                    'symptoms': ['same deployment restarts every few '
                                                                                 'minutes',
                                                                                 'crash follows config map or secret '
                                                                                 'update',
                                                                                 'only one namespace affected'],
                                                                    'causes': ['bad config or secret value',
                                                                               'readiness dependency unavailable',
                                                                               'application process exits on startup '
                                                                               'due to missing env var'],
                                                                    'checks': ['check pod logs and restart reasons',
                                                                               'review config map or secret diff',
                                                                               'validate dependent services reachable '
                                                                               'from namespace'],
                                                                    'actions': ['rollback bad config',
                                                                                'restore required secret',
                                                                                'pause rollout until dependency '
                                                                                'healthy'],
                                                                    'automation_candidate': True},
                                                 'image_pull_failure': {'title': 'Container image pull fails',
                                                                        'symptoms': ['new pods remain in '
                                                                                     'ImagePullBackOff',
                                                                                     'registry reachable from control '
                                                                                     'plane but not nodes',
                                                                                     'issue starts after image '
                                                                                     'promotion'],
                                                                        'causes': ['image tag missing',
                                                                                   'registry auth secret expired',
                                                                                   'egress or dns issue from worker '
                                                                                   'nodes'],
                                                                        'checks': ['check image tag exists in registry',
                                                                                   'validate pull secret',
                                                                                   'review node connectivity to '
                                                                                   'registry'],
                                                                        'actions': ['republish missing tag',
                                                                                    'refresh registry pull secret',
                                                                                    'restore egress path to registry'],
                                                                        'automation_candidate': True},
                                                 'node_not_ready': {'title': 'Cluster node not ready',
                                                                    'symptoms': ['pods drained from one worker '
                                                                                 'unexpectedly',
                                                                                 'kubelet heartbeat stops',
                                                                                 'node experienced resource pressure '
                                                                                 'or network isolate'],
                                                                    'causes': ['kubelet failure',
                                                                               'node disk pressure',
                                                                               'cni or network issue'],
                                                                    'checks': ['check kubelet status',
                                                                               'review node conditions and events',
                                                                               'validate cni plugin and node '
                                                                               'reachability'],
                                                                    'actions': ['restart kubelet on affected node',
                                                                                'free node disk space',
                                                                                'cordon and recycle unhealthy node'],
                                                                    'automation_candidate': True},
                                                 'service_mesh_latency': {'title': 'Service mesh latency spike',
                                                                          'symptoms': ['inter-service calls slower '
                                                                                       'with retries',
                                                                                       'proxy sidecar cpu increases',
                                                                                       'latency disappears when '
                                                                                       'bypassing mesh in test path'],
                                                                          'causes': ['sidecar resource starvation',
                                                                                     'mesh policy or mTLS issue',
                                                                                     'control plane push backlog'],
                                                                          'checks': ['review sidecar metrics',
                                                                                     'check mesh control plane health',
                                                                                     'validate recent policy or '
                                                                                     'certificate changes'],
                                                                          'actions': ['increase sidecar resources',
                                                                                      'rollback recent mesh policy '
                                                                                      'change',
                                                                                      'restart degraded control plane '
                                                                                      'component'],
                                                                          'automation_candidate': False},
                                                 'dns_in_cluster_failure': {'title': 'Cluster DNS failing for internal '
                                                                                     'service names',
                                                                            'symptoms': ['pods cannot resolve service '
                                                                                         'names',
                                                                                         'external resolution still '
                                                                                         'works',
                                                                                         'issue tied to core dns '
                                                                                         'restart storm or config '
                                                                                         'change'],
                                                                            'causes': ['CoreDNS config error',
                                                                                       'upstream dns dependency issue',
                                                                                       'node local dns cache '
                                                                                       'inconsistency'],
                                                                            'checks': ['check CoreDNS logs and pods',
                                                                                       'validate upstream dns '
                                                                                       'availability',
                                                                                       'compare node-local cache '
                                                                                       'behavior'],
                                                                            'actions': ['rollback CoreDNS config',
                                                                                        'restart unhealthy CoreDNS '
                                                                                        'pods',
                                                                                        'flush node local dns caches'],
                                                                            'automation_candidate': True},
                                                 'ingress_certificate_mismatch': {'title': 'Ingress certificate '
                                                                                           'mismatch or handshake '
                                                                                           'failure',
                                                                                  'symptoms': ['clients receive wrong '
                                                                                               'certificate on app '
                                                                                               'endpoint',
                                                                                               'only one ingress '
                                                                                               'gateway affected',
                                                                                               'issue follows '
                                                                                               'certificate renewal'],
                                                                                  'causes': ['secret updated in wrong '
                                                                                             'namespace',
                                                                                             'ingress controller using '
                                                                                             'stale certificate cache',
                                                                                             'SNI host mapping drift'],
                                                                                  'checks': ['check ingress secret '
                                                                                             'references',
                                                                                             'review controller reload '
                                                                                             'events',
                                                                                             'validate host to '
                                                                                             'certificate mapping'],
                                                                                  'actions': ['reload ingress '
                                                                                              'controller',
                                                                                              'correct certificate '
                                                                                              'secret mapping',
                                                                                              'redeploy ingress '
                                                                                              'gateway with current '
                                                                                              'cert bundle'],
                                                                                  'automation_candidate': True}}},
 'Middleware & Integration': {'domain': 'middleware',
                              'weight': 0.03,
                              'team': 'Middleware Operations',
                              'services': ['Kafka', 'MQ', 'API Gateway', 'ETL', 'Integration Bus'],
                              'classifications': {'queue_backlog_growth': {'title': 'Queue backlog increasing beyond '
                                                                                    'normal threshold',
                                                                           'symptoms': ['downstream processing delayed',
                                                                                        'consumer lag rises while '
                                                                                        'producers remain steady',
                                                                                        'business reconciliation jobs '
                                                                                        'miss SLA'],
                                                                           'causes': ['consumer pod or service '
                                                                                      'degradation',
                                                                                      'schema validation failure '
                                                                                      'causing retries',
                                                                                      'storage latency on broker '
                                                                                      'nodes'],
                                                                           'checks': ['check consumer lag by topic or '
                                                                                      'queue',
                                                                                      'review broker and consumer logs',
                                                                                      'validate recent schema changes'],
                                                                           'actions': ['scale consumers',
                                                                                       'pause bad producer or route to '
                                                                                       'dead-letter queue',
                                                                                       'restart degraded broker or '
                                                                                       'consumer group'],
                                                                           'automation_candidate': True},
                                                  'api_gateway_5xx_spike': {'title': 'API gateway 5xx errors spike',
                                                                            'symptoms': ['external callers receive '
                                                                                         'intermittent 502 or 504',
                                                                                         'single backend pool appears '
                                                                                         'slower',
                                                                                         'gateway config changed '
                                                                                         'during release window'],
                                                                            'causes': ['gateway timeout too low',
                                                                                       'upstream backend latency',
                                                                                       'gateway plugin regression'],
                                                                            'checks': ['review gateway access and '
                                                                                       'error logs',
                                                                                       'check backend response times',
                                                                                       'compare current gateway config '
                                                                                       'to baseline'],
                                                                            'actions': ['rollback plugin or config '
                                                                                        'change',
                                                                                        'increase timeout temporarily',
                                                                                        'drain unhealthy backend from '
                                                                                        'gateway routing'],
                                                                            'automation_candidate': True},
                                                  'message_schema_validation_failure': {'title': 'Integration messages '
                                                                                                 'fail schema '
                                                                                                 'validation',
                                                                                        'symptoms': ['new payload '
                                                                                                     'version rejected '
                                                                                                     'by consumer',
                                                                                                     'DLQ volume rises '
                                                                                                     'after partner '
                                                                                                     'change',
                                                                                                     'only one '
                                                                                                     'business flow '
                                                                                                     'affected'],
                                                                                        'causes': ['schema version '
                                                                                                   'mismatch',
                                                                                                   'producer sent '
                                                                                                   'malformed payload',
                                                                                                   'consumer '
                                                                                                   'compatibility flag '
                                                                                                   'not updated'],
                                                                                        'checks': ['check schema '
                                                                                                   'registry versions',
                                                                                                   'review DLQ payload '
                                                                                                   'samples',
                                                                                                   'validate producer '
                                                                                                   'release notes'],
                                                                                        'actions': ['roll back '
                                                                                                    'incompatible '
                                                                                                    'producer version',
                                                                                                    'update consumer '
                                                                                                    'schema '
                                                                                                    'compatibility',
                                                                                                    'replay corrected '
                                                                                                    'messages from '
                                                                                                    'DLQ'],
                                                                                        'automation_candidate': False},
                                                  'mq_channel_down': {'title': 'MQ channel down between applications',
                                                                      'symptoms': ['messages accumulate on sender side',
                                                                                   'channel retries increase',
                                                                                   'network path appears stable while '
                                                                                   'auth errors appear'],
                                                                      'causes': ['channel auth mismatch',
                                                                                 'certificate expiry on MQ pair',
                                                                                 'queue manager restart incomplete'],
                                                                      'checks': ['check MQ channel status and logs',
                                                                                 'validate channel certificates and '
                                                                                 'auth config',
                                                                                 'review queue manager health'],
                                                                      'actions': ['restart affected channel',
                                                                                  'renew channel certificates',
                                                                                  'correct auth mapping and reopen '
                                                                                  'channel'],
                                                                      'automation_candidate': True}}},
 'Storage & Backup': {'domain': 'storage_backup',
                      'weight': 0.04,
                      'team': 'Storage Services',
                      'services': ['SAN', 'NAS', 'Backup Platform', 'Archive', 'Object Storage'],
                      'classifications': {'storage_latency_spike': {'title': 'Storage latency exceeds application '
                                                                             'threshold',
                                                                    'symptoms': ['database or VDI latency alarms '
                                                                                 'correlate with SAN metrics',
                                                                                 'one storage tier impacted more than '
                                                                                 'others',
                                                                                 'issue follows replication or '
                                                                                 'snapshot window'],
                                                                    'causes': ['snapshot contention',
                                                                               'storage controller pressure',
                                                                               'replication burst saturating cache'],
                                                                    'checks': ['check array latency by volume',
                                                                               'review snapshot and replication jobs',
                                                                               'compare controller utilization'],
                                                                    'actions': ['throttle snapshot schedule',
                                                                                'move critical workload to '
                                                                                'lower-latency tier',
                                                                                'rebalance controller workload'],
                                                                    'automation_candidate': False},
                                          'nas_share_unavailable': {'title': 'NAS share unavailable or slow',
                                                                    'symptoms': ['mounted file shares disappear or '
                                                                                 'time out',
                                                                                 'access works from one subnet but not '
                                                                                 'another',
                                                                                 'same share impacted after ACL or '
                                                                                 'export change'],
                                                                    'causes': ['nas interface failover issue',
                                                                               'permission change error',
                                                                               'directory service dependency for share '
                                                                               'auth failed'],
                                                                    'checks': ['check NAS interface and failover '
                                                                               'events',
                                                                               'validate share export and ACL settings',
                                                                               'review directory integration health'],
                                                                    'actions': ['fail over NAS interface cleanly',
                                                                                'restore share ACLs',
                                                                                'restart NAS directory auth service'],
                                                                    'automation_candidate': True},
                                          'backup_repository_full': {'title': 'Backup repository capacity nearly full',
                                                                     'symptoms': ['new backups fail or synthetic fulls '
                                                                                  'skip',
                                                                                  'retention cleanup did not reclaim '
                                                                                  'expected space',
                                                                                  'same repository serves multiple '
                                                                                  'business tiers'],
                                                                     'causes': ['retention cleanup failure',
                                                                                'unexpected data growth',
                                                                                'orphaned restore points'],
                                                                     'checks': ['check repository utilization trend',
                                                                                'review cleanup and retention job logs',
                                                                                'identify orphaned backup chains'],
                                                                     'actions': ['delete orphaned restore points where '
                                                                                 'approved',
                                                                                 'extend repository capacity',
                                                                                 'rebalance backup jobs across '
                                                                                 'repositories'],
                                                                     'automation_candidate': True},
                                          'restore_job_failure': {'title': 'Restore job fails during validation or '
                                                                           'recovery',
                                                                  'symptoms': ['test restore misses SLA',
                                                                               'recovery starts but target mount fails',
                                                                               'issue limited to one backup type'],
                                                                  'causes': ['backup chain inconsistency',
                                                                             'target environment permissions issue',
                                                                             'media or object retrieval timeout'],
                                                                  'checks': ['review restore job logs',
                                                                             'validate backup chain integrity',
                                                                             'check target permissions and mount '
                                                                             'health'],
                                                                  'actions': ['repair backup chain metadata',
                                                                              'retry restore to alternate target',
                                                                              'fix target permissions and rerun '
                                                                              'validation'],
                                                                  'automation_candidate': False}}},
 'Security Operations': {'domain': 'security',
                         'weight': 0.05,
                         'team': 'Security Operations',
                         'services': ['SIEM', 'EDR', 'Email Security', 'WAF', 'PAM'],
                         'classifications': {'certificate_expiry_warning': {'title': 'Certificate near expiry on '
                                                                                     'critical endpoint',
                                                                            'symptoms': ['monitoring shows less than '
                                                                                         'fourteen days remaining',
                                                                                         'same certificate used across '
                                                                                         'multiple services',
                                                                                         'renewal job did not update '
                                                                                         'bound endpoint'],
                                                                            'causes': ['renewal automation failed',
                                                                                       'certificate bound to wrong '
                                                                                       'service',
                                                                                       'chain or private key mismatch '
                                                                                       'after renewal'],
                                                                            'checks': ['check certificate expiry '
                                                                                       'inventory',
                                                                                       'validate renewal automation '
                                                                                       'logs',
                                                                                       'compare live cert with '
                                                                                       'expected keystore'],
                                                                            'actions': ['renew certificate',
                                                                                        'bind renewed certificate to '
                                                                                        'service',
                                                                                        'restart service to load new '
                                                                                        'certificate'],
                                                                            'automation_candidate': True},
                                             'suspicious_login_pattern': {'title': 'Suspicious authentication pattern '
                                                                                   'detected',
                                                                          'symptoms': ['multiple failed attempts from '
                                                                                       'unusual geography',
                                                                                       'same user logs in from '
                                                                                       'impossible travel sequence',
                                                                                       'MFA challenge fatigue '
                                                                                       'reported'],
                                                                          'causes': ['credential stuffing attempt',
                                                                                     'compromised account or token',
                                                                                     'false positive due to proxy '
                                                                                     'egress change'],
                                                                          'checks': ['review sign-in telemetry and '
                                                                                     'impossible travel details',
                                                                                     'check MFA challenge logs',
                                                                                     'validate proxy egress geo '
                                                                                     'changes'],
                                                                          'actions': ['force password reset and revoke '
                                                                                      'sessions',
                                                                                      'block suspicious source ranges',
                                                                                      'escalate for threat '
                                                                                      'investigation'],
                                                                          'automation_candidate': True},
                                             'endpoint_malware_alert': {'title': 'Endpoint malware alert raised',
                                                                        'symptoms': ['EDR detects malicious behavior '
                                                                                     'or quarantines file',
                                                                                     'same hash appears on several '
                                                                                     'endpoints',
                                                                                     'affected users report slowness '
                                                                                     'or popups'],
                                                                        'causes': ['malicious file execution',
                                                                                   'false positive detection',
                                                                                   'lateral spread via shared drive'],
                                                                        'checks': ['review EDR alert details',
                                                                                   'check file hash prevalence',
                                                                                   'validate user actions before '
                                                                                   'alert'],
                                                                        'actions': ['isolate endpoint',
                                                                                    'quarantine malicious file',
                                                                                    'collect forensic package and '
                                                                                    'reimage if confirmed'],
                                                                        'automation_candidate': True},
                                             'privilege_escalation_attempt': {'title': 'Privilege escalation suspected '
                                                                                       'on endpoint or server',
                                                                              'symptoms': ['unexpected admin group '
                                                                                           'change',
                                                                                           'EDR flags token '
                                                                                           'manipulation',
                                                                                           'sensitive commands '
                                                                                           'executed outside '
                                                                                           'maintenance window'],
                                                                              'causes': ['malicious escalation attempt',
                                                                                         'misconfigured automation '
                                                                                         'running elevated',
                                                                                         'legitimate admin task '
                                                                                         'outside change process'],
                                                                              'checks': ['review admin group change '
                                                                                         'logs',
                                                                                         'check EDR process lineage',
                                                                                         'correlate with change record '
                                                                                         'or automation history'],
                                                                              'actions': ['disable compromised '
                                                                                          'credentials',
                                                                                          'isolate impacted host',
                                                                                          'escalate to incident '
                                                                                          'response team'],
                                                                              'automation_candidate': False},
                                             'waf_false_positive_block': {'title': 'Web application firewall blocking '
                                                                                   'legitimate traffic',
                                                                          'symptoms': ['customers receive 403 on valid '
                                                                                       'requests',
                                                                                       'new API path affected after '
                                                                                       'ruleset update',
                                                                                       'application logs show no '
                                                                                       'matching backend request'],
                                                                          'causes': ['waf signature false positive',
                                                                                     'custom rule too strict',
                                                                                     'request format changed after '
                                                                                     'release'],
                                                                          'checks': ['review WAF block logs and '
                                                                                     'signatures',
                                                                                     'compare blocked versus allowed '
                                                                                     'requests',
                                                                                     'validate recent app release '
                                                                                     'changes'],
                                                                          'actions': ['tune WAF rule exception',
                                                                                      'roll back overly strict custom '
                                                                                      'signature',
                                                                                      'whitelist validated request '
                                                                                      'pattern'],
                                                                          'automation_candidate': False},
                                             'siem_ingestion_gap': {'title': 'SIEM ingestion gap detected',
                                                                    'symptoms': ['security dashboards missing fresh '
                                                                                 'events',
                                                                                 'one log source type delayed more '
                                                                                 'than others',
                                                                                 'pipeline lag started after parser '
                                                                                 'update'],
                                                                    'causes': ['parser error',
                                                                               'collector queue backlog',
                                                                               'storage or index pressure in SIEM'],
                                                                    'checks': ['check ingestion pipeline health',
                                                                               'review parser errors',
                                                                               'validate index or storage capacity'],
                                                                    'actions': ['restart or scale collector',
                                                                                'rollback parser change',
                                                                                'increase SIEM index capacity'],
                                                                    'automation_candidate': True}}},
 'Application Support': {'domain': 'application_support',
                         'weight': 0.07,
                         'team': 'Application Operations',
                         'services': ['Customer Apps',
                                      'Internal Apps',
                                      'Batch Jobs',
                                      'Web Portals',
                                      'Integration APIs'],
                         'classifications': {'application_latency_spike': {'title': 'Application latency spike '
                                                                                    'impacting users',
                                                                           'symptoms': ['response time above SLA for '
                                                                                        'key transaction',
                                                                                        'only one region or node pool '
                                                                                        'affected',
                                                                                        'latency follows deployment or '
                                                                                        'traffic burst'],
                                                                           'causes': ['backend dependency slowdown',
                                                                                      'thread pool saturation',
                                                                                      'inefficient query or cache miss '
                                                                                      'storm'],
                                                                           'checks': ['check app APM traces',
                                                                                      'review thread pool and '
                                                                                      'dependency metrics',
                                                                                      'compare release markers to '
                                                                                      'latency onset'],
                                                                           'actions': ['restart affected app node',
                                                                                       'scale application tier',
                                                                                       'rollback recent release if '
                                                                                       'regression confirmed'],
                                                                           'automation_candidate': True},
                                             'http_500_error_burst': {'title': 'HTTP 500 errors increase after release',
                                                                      'symptoms': ['error rate spikes for one endpoint',
                                                                                   'synthetic checks fail '
                                                                                   'intermittently',
                                                                                   'issue absent in previous release '
                                                                                   'candidate'],
                                                                      'causes': ['application bug introduced in '
                                                                                 'release',
                                                                                 'config mismatch between nodes',
                                                                                 'dependency timeout not handled'],
                                                                      'checks': ['review application error logs',
                                                                                 'compare node config checksums',
                                                                                 'check dependency availability and '
                                                                                 'timeout metrics'],
                                                                      'actions': ['rollback faulty release',
                                                                                  'reconcile node configuration',
                                                                                  'increase timeout while root cause '
                                                                                  'fixed'],
                                                                      'automation_candidate': True},
                                             'cache_cluster_issue': {'title': 'Cache cluster not serving consistent '
                                                                              'results',
                                                                     'symptoms': ['users receive stale data',
                                                                                  'cache miss rate and backend load '
                                                                                  'rise together',
                                                                                  'one cache shard reports failover '
                                                                                  'recently'],
                                                                     'causes': ['cache node failure',
                                                                                'replication lag or split brain',
                                                                                'eviction policy too aggressive'],
                                                                     'checks': ['check cache cluster health',
                                                                                'review replication and failover '
                                                                                'status',
                                                                                'validate memory and eviction metrics'],
                                                                     'actions': ['fail over unhealthy cache node',
                                                                                 'rebuild impacted shard',
                                                                                 'tune cache eviction policy'],
                                                                     'automation_candidate': True},
                                             'batch_job_failure': {'title': 'Business batch job failed or exceeded '
                                                                            'processing window',
                                                                   'symptoms': ['downstream files not produced',
                                                                                'job retries on same step repeatedly',
                                                                                'month-end workload intensifies issue'],
                                                                   'causes': ['dependency file missing',
                                                                              'resource contention on batch host',
                                                                              'job logic error after code change'],
                                                                   'checks': ['review scheduler and batch logs',
                                                                              'check inbound dependency files',
                                                                              'compare code and config changes to '
                                                                              'previous successful run'],
                                                                   'actions': ['rerun batch after dependency restored',
                                                                               'move batch to healthy host',
                                                                               'roll back code change affecting job '
                                                                               'step'],
                                                                   'automation_candidate': False},
                                             'feature_flag_misconfiguration': {'title': 'Feature flag or config toggle '
                                                                                        'causing user impact',
                                                                               'symptoms': ['only subset of users see '
                                                                                            'error',
                                                                                            'issue disappears when '
                                                                                            'feature flag disabled',
                                                                                            'flag state differs across '
                                                                                            'environments'],
                                                                               'causes': ['flag enabled without '
                                                                                          'dependency',
                                                                                          'targeting rule too broad',
                                                                                          'stale config cache on app '
                                                                                          'nodes'],
                                                                               'checks': ['check feature flag settings',
                                                                                          'compare environment '
                                                                                          'configuration',
                                                                                          'validate config cache '
                                                                                          'refresh timing'],
                                                                               'actions': ['disable faulty feature '
                                                                                           'flag',
                                                                                           'narrow targeting rule',
                                                                                           'force config refresh on '
                                                                                           'app nodes'],
                                                                               'automation_candidate': True},
                                             'job_scheduler_missed_trigger': {'title': 'Job scheduler missed or '
                                                                                       'duplicated trigger',
                                                                              'symptoms': ['scheduled process did not '
                                                                                           'run at expected time',
                                                                                           'job executed twice after '
                                                                                           'failover',
                                                                                           'scheduler clock drift seen '
                                                                                           'on one node'],
                                                                              'causes': ['scheduler cluster split '
                                                                                         'brain',
                                                                                         'time drift on scheduler node',
                                                                                         'trigger store lock '
                                                                                         'contention'],
                                                                              'checks': ['review scheduler cluster '
                                                                                         'state',
                                                                                         'check node time '
                                                                                         'synchronization',
                                                                                         'validate trigger store '
                                                                                         'health'],
                                                                              'actions': ['reconcile scheduler cluster '
                                                                                          'leadership',
                                                                                          'restart affected scheduler '
                                                                                          'node',
                                                                                          'repair trigger store lock '
                                                                                          'contention'],
                                                                              'automation_candidate': True}}},
 'Change & Release': {'domain': 'change_release',
                      'weight': 0.02,
                      'team': 'Release Management',
                      'services': ['CI/CD', 'Deployment Automation', 'Configuration Management', 'Change Control'],
                      'classifications': {'post_change_instability': {'title': 'Service instability observed after '
                                                                               'approved change',
                                                                      'symptoms': ['users report intermittent errors '
                                                                                   'after deployment',
                                                                                   'monitoring shows multiple signals '
                                                                                   'but no single obvious cause',
                                                                                   'rollback improves service quality'],
                                                                      'causes': ['release artifact defect',
                                                                                 'config drift introduced during '
                                                                                 'deployment',
                                                                                 'dependency compatibility mismatch'],
                                                                      'checks': ['review change implementation details',
                                                                                 'compare deployed config with '
                                                                                 'baseline',
                                                                                 'validate rollback outcome against '
                                                                                 'metrics'],
                                                                      'actions': ['rollback change',
                                                                                  'pause further rollout',
                                                                                  'open post-implementation review '
                                                                                  'with evidence'],
                                                                      'automation_candidate': False},
                                          'deployment_pipeline_failure': {'title': 'Deployment pipeline fails during '
                                                                                   'release stage',
                                                                          'symptoms': ['build artifacts created but '
                                                                                       'deploy stage aborts',
                                                                                       'same pipeline works in test '
                                                                                       'but not prod',
                                                                                       'credential or approval step '
                                                                                       'recently changed'],
                                                                          'causes': ['expired deployment credential',
                                                                                     'pipeline variable '
                                                                                     'misconfiguration',
                                                                                     'target environment validation '
                                                                                     'failure'],
                                                                          'checks': ['review pipeline execution logs',
                                                                                     'validate deployment credentials '
                                                                                     'and approvals',
                                                                                     'check target environment '
                                                                                     'readiness gates'],
                                                                          'actions': ['rotate deployment credential',
                                                                                      'fix pipeline variable mapping',
                                                                                      'retry deployment after target '
                                                                                      'validation passes'],
                                                                          'automation_candidate': True},
                                          'configuration_drift_detected': {'title': 'Configuration drift detected '
                                                                                    'across environment',
                                                                           'symptoms': ['one node behaves differently '
                                                                                        'from peers',
                                                                                        'IaC shows manual changes on '
                                                                                        'managed asset',
                                                                                        'issue persists after '
                                                                                        'application restart'],
                                                                           'causes': ['manual hotfix left in place',
                                                                                      'config management agent failed',
                                                                                      'baseline not enforced after '
                                                                                      'server rebuild'],
                                                                           'checks': ['compare node configuration to '
                                                                                      'desired state',
                                                                                      'review config management last '
                                                                                      'run',
                                                                                      'identify manual changes outside '
                                                                                      'change record'],
                                                                           'actions': ['reapply desired state config',
                                                                                       'restart config management '
                                                                                       'agent',
                                                                                       'remove unauthorized manual '
                                                                                       'changes'],
                                                                           'automation_candidate': True},
                                          'release_dependency_missing': {'title': 'Release dependency not promoted '
                                                                                  'with application change',
                                                                         'symptoms': ['app deploy succeeds but feature '
                                                                                      'fails at runtime',
                                                                                      'schema or service contract '
                                                                                      'mismatch appears immediately',
                                                                                      'issue isolated to current '
                                                                                      'release train'],
                                                                         'causes': ['database or API dependency not '
                                                                                    'promoted',
                                                                                    'feature flag dependency missed',
                                                                                    'package version mismatch in '
                                                                                    'release bundle'],
                                                                         'checks': ['review release checklist and '
                                                                                    'manifest',
                                                                                    'compare dependency versions '
                                                                                    'across environments',
                                                                                    'check schema or contract '
                                                                                    'compatibility'],
                                                                         'actions': ['promote missing dependency',
                                                                                     'rollback incomplete release '
                                                                                     'bundle',
                                                                                     'enforce dependency gate in '
                                                                                     'pipeline'],
                                                                         'automation_candidate': False}}}}

SERVICE_KEYWORD_MAP = [('teams', ['Teams', 'Video Conferencing']),
 ('outlook', ['Outlook Desktop', 'Exchange Online']),
 ('mailbox', ['Outlook Desktop', 'Exchange Online']),
 ('onedrive', ['OneDrive']),
 ('sharepoint', ['SharePoint']),
 ('calendar', ['Exchange Online', 'Teams']),
 ('vpn', ['VPN Client', 'Remote Access', 'VPN']),
 ('printer', ['Printer Services']),
 ('citrix', ['Citrix', 'VDI']),
 ('vdi', ['VDI', 'Citrix']),
 ('bitlocker', ['BitLocker']),
 ('wifi', ['Wi-Fi']),
 ('endpoint', ['Endpoint Security Agent', 'EDR']),
 ('dns', ['DNS', 'Kubernetes']),
 ('database', ['SQL Server', 'Oracle', 'PostgreSQL', 'MySQL']),
 ('mq', ['MQ']),
 ('queue', ['Kafka', 'MQ']),
 ('api', ['API Gateway', 'Integration APIs']),
 ('certificate', ['WAF', 'Ingress', 'Entra ID']),
 ('cache', ['Customer Apps', 'Internal Apps']),
 ('wireless', ['LAN']),
 ('load balancer', ['Load Balancers']),
 ('firewall', ['Firewall']),
 ('proxy', ['Proxy']),
 ('exchange', ['Exchange Online']),
 ('mail', ['Exchange Online', 'Outlook Desktop']),
 ('sharepoint', ['SharePoint']),
 ('identity', ['Entra ID', 'Active Directory']),
 ('account', ['Active Directory', 'Entra ID']),
 ('directory', ['Active Directory']),
 ('privileged', ['Privileged Access', 'PAM']),
 ('federation', ['Federation Services', 'SSO Integrations']),
 ('cluster', ['Kubernetes', 'OpenShift', 'Service Mesh']),
 ('ingress', ['Ingress']),
 ('image', ['Container Registry', 'Kubernetes']),
 ('storage', ['SAN', 'NAS', 'Object Storage']),
 ('backup', ['Backup Platform']),
 ('restore', ['Backup Platform']),
 ('cache', ['Internal Apps', 'Customer Apps']),
 ('batch', ['Batch Jobs']),
 ('feature', ['Customer Apps', 'Internal Apps']),
 ('release', ['Deployment Automation', 'CI/CD']),
 ('deployment', ['CI/CD', 'Deployment Automation']),
 ('mfa', ['SSO Portal', 'Entra ID']),
 ('password', ['Service Portal', 'Active Directory', 'Entra ID']),
 ('shared mailbox', ['Outlook Desktop', 'Exchange Online']),
 ('software install', ['Service Portal', 'Windows 11 Fleet']),
 ('quarantine', ['Exchange Online', 'Email Security']),
 ('authentication', ['SIEM', 'Entra ID']),
 ('login', ['Entra ID', 'PAM', 'EDR']),
 ('suspicious', ['SIEM', 'EDR']),
 ('malware', ['EDR']),
 ('profile', ['Windows 11 Fleet', 'VDI', 'Citrix']),
 ('disk', ['Windows Servers', 'Linux Servers', 'Cloud Networking']),
 ('group membership', ['Active Directory', 'Entra ID']),
 ('lockout', ['Active Directory', 'Entra ID']),
 ('service account', ['Active Directory', 'Privileged Access']),
 ('printer', ['Printer Services', 'Windows 11 Fleet']),
 ('boot', ['Windows 11 Fleet', 'Windows Servers']),
 ('battery', ['Windows 11 Fleet']),
 ('docking', ['Docking Stations']),
 ('audio', ['Teams'])]
OFFICE_LOCATIONS = ['London HQ',
 'New York Office',
 'Mumbai Delivery Center',
 'Bengaluru Campus',
 'Singapore Office',
 'Frankfurt DC',
 'Toronto Office',
 'Remote Workforce',
 'Sydney Office',
 'Dubai Office']
BUSINESS_UNITS = ['Retail Banking',
 'Claims Operations',
 'Finance Shared Services',
 'HR Operations',
 'Trading Support',
 'Customer Care',
 'Manufacturing',
 'Corporate Functions',
 'Digital Channels',
 'Field Services']
IMPACTED_GROUPS = ['single user', 'multiple users', 'department', 'site', 'business unit', 'regional users', 'global users']
PLATFORMS = ['Windows', 'Linux', 'Azure', 'AWS', 'VMware', 'Citrix', 'Kubernetes', 'Oracle', 'SQL Server', 'M365']
CHANGE_CONTEXT = ['after a routine maintenance window',
 'following a security patch cycle',
 'after a standard monthly release',
 'after a certificate rollover',
 'during the morning login peak',
 'after endpoint policy refresh',
 'shortly after a network rule cleanup',
 'after a platform scaling event']
SEVERITY_WEIGHTS = [('low', 0.18), ('medium', 0.47), ('high', 0.27), ('critical', 0.08)]
ENVIRONMENT_WEIGHTS = [('prod', 0.67), ('uat', 0.13), ('dev', 0.08), ('dr', 0.04), ('preprod', 0.08)]
RECENCY_BUCKETS = [('recent', 0, 30), ('active', 31, 90), ('mature', 91, 240), ('stale', 241, 540)]
DESCRIPTION_PATTERNS = ['{impacted} in {location} reported that {title_l} {change_ctx}. The issue affected {service} within the '
 '{business_unit} landscape. Initial triage suggests {cause1}, while repeated symptoms indicate {cause2}.',
 'Monitoring and user reports show {title_l} for {impacted} across {location}. The incident surfaced {change_ctx} on '
 'the {platform} estate supporting {service}. Working theory points to {cause1} together with {cause2}.',
 'Operations teams observed that {title_l} impacting {impacted} tied to {service} in {location}. The event began '
 '{change_ctx} and correlates with signals commonly associated with {cause1} and {cause2}.',
 'Enterprise support received multiple alerts because {title_l} affecting {impacted} in {location}. The pattern '
 'emerged {change_ctx} and aligns with {cause1}; secondary evidence also points to {cause2}.']
USER_NOTES = ['Users report the issue is intermittent but worsening during peak hours.',
 'A workaround is temporarily successful but the fault returns after reconnect or restart.',
 'Similar behavior has been observed before when configuration drift was introduced.',
 'Synthetic monitoring and user impact started within the same thirty-minute window.',
 'The issue appears isolated to a subset of assets but has potential to spread to adjacent services.']
PRIORITY_MAP = {"low": "P4", "medium": "P3", "high": "P2", "critical": "P1"}


def weighted_choice(weighted_items, rng):
    total = sum(weight for _, weight in weighted_items)
    pick = rng.random() * total
    upto = 0.0
    for item, weight in weighted_items:
        upto += weight
        if upto >= pick:
            return item
    return weighted_items[-1][0]


def choose_severity(rng, scope_name: str, cls_key: str) -> str:
    adjusted = dict(SEVERITY_WEIGHTS)
    if scope_name in {"Security Operations", "Network", "Kubernetes & Containers", "Cloud Platform"}:
        adjusted["high"] += 0.05
        adjusted["critical"] += 0.03
        adjusted["medium"] -= 0.05
        adjusted["low"] -= 0.03
    if scope_name in {"Service Desk", "End User Computing", "Messaging & Collaboration"}:
        adjusted["medium"] += 0.04
        adjusted["high"] -= 0.02
        adjusted["critical"] -= 0.01
        adjusted["low"] -= 0.01
    if cls_key in {"endpoint_malware_alert", "privilege_escalation_attempt", "certificate_expiry_warning"}:
        adjusted["critical"] += 0.08
        adjusted["high"] += 0.04
        adjusted["medium"] -= 0.08
        adjusted["low"] -= 0.04
    return weighted_choice(list(adjusted.items()), rng)


def choose_environment(rng, scope_name: str) -> str:
    env = weighted_choice(ENVIRONMENT_WEIGHTS, rng)
    if scope_name in {"Service Desk", "End User Computing"} and rng.random() < 0.65:
        return "prod"
    return env


def compute_staleness_score(age_days: int) -> float:
    return round(min(1.0, age_days / 540.0), 3)


def recency_bucket(age_days: int) -> str:
    for label, lo, hi in RECENCY_BUCKETS:
        if lo <= age_days <= hi:
            return label
    return "stale"


def choose_service(scope_services, title: str, cls_key: str, rng) -> str:
    hay = f"{title} {cls_key}".lower()
    candidates = []
    for keyword, services in SERVICE_KEYWORD_MAP:
        if keyword in hay:
            candidates.extend(services)
    candidates = [service for service in candidates if service in scope_services]
    if candidates:
        return rng.choice(candidates)
    return rng.choice(scope_services)


def choose_platform(scope_name: str, rng, cls_key: str) -> str:
    if scope_name == "End User Computing":
        return rng.choice(["Windows", "Citrix", "VMware"])
    if scope_name == "Messaging & Collaboration":
        return rng.choice(["M365", "Windows", "Azure"])
    if scope_name == "Database":
        return rng.choice(["Oracle", "SQL Server", "Linux", "Windows"])
    if scope_name == "Kubernetes & Containers":
        return rng.choice(["Kubernetes", "Linux", "AWS", "Azure"])
    if scope_name == "Cloud Platform":
        return rng.choice(["AWS", "Azure", "GCP", "Linux"])
    if scope_name == "Service Desk":
        return rng.choice(["Windows", "M365", "Citrix"])
    if scope_name == "Security Operations":
        return rng.choice(["Windows", "Linux", "M365", "Azure"])
    return rng.choice(PLATFORMS)


def generate_records(count: int = 5000, seed: int = 20260412):
    rng = random.Random(seed)
    scope_weights = [(scope_name, scope["weight"]) for scope_name, scope in SERVICE_SCOPE_CATALOG.items()]
    raw_counts = [(scope_name, weight * count) for scope_name, weight in scope_weights]
    scope_counts = {scope_name: int(value) for scope_name, value in raw_counts}
    remaining = count - sum(scope_counts.values())
    remainders = sorted(((value - int(value), scope_name) for scope_name, value in raw_counts), reverse=True)
    for _, scope_name in remainders[:remaining]:
        scope_counts[scope_name] += 1

    records = []
    seq = 100000
    for scope_name, n in scope_counts.items():
        scope = SERVICE_SCOPE_CATALOG[scope_name]
        class_items = list(scope["classifications"].items())
        class_weights = []
        for idx, (class_key, _) in enumerate(class_items):
            base = 1.7 if idx < 3 else 1.2 if idx < 6 else 1.0
            class_weights.append((class_key, base))

        for _ in range(n):
            class_key = weighted_choice(class_weights, rng)
            class_cfg = scope["classifications"][class_key]

            age_days = rng.randint(1, 540)
            created_at = (
                datetime.now(timezone.utc)
                - timedelta(days=age_days, hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
            ).isoformat()

            severity = choose_severity(rng, scope_name, class_key)
            environment = choose_environment(rng, scope_name)
            priority = PRIORITY_MAP[severity]
            location = rng.choice(OFFICE_LOCATIONS)
            business_unit = rng.choice(BUSINESS_UNITS)
            impacted_scope = rng.choice(IMPACTED_GROUPS)
            platform = choose_platform(scope_name, rng, class_key)
            service = choose_service(scope["services"], class_cfg["title"], class_key, rng)

            cause1, cause2 = rng.sample(class_cfg["causes"], 2) if len(class_cfg["causes"]) >= 2 else (class_cfg["causes"][0], class_cfg["causes"][0])
            check1, check2 = rng.sample(class_cfg["checks"], 2) if len(class_cfg["checks"]) >= 2 else (class_cfg["checks"][0], class_cfg["checks"][0])
            action1, action2 = rng.sample(class_cfg["actions"], 2) if len(class_cfg["actions"]) >= 2 else (class_cfg["actions"][0], class_cfg["actions"][0])
            symptom1 = rng.choice(class_cfg["symptoms"])
            symptom2 = rng.choice([symptom for symptom in class_cfg["symptoms"] if symptom != symptom1] or class_cfg["symptoms"])

            title = class_cfg["title"]
            description = rng.choice(DESCRIPTION_PATTERNS).format(
                impacted=impacted_scope,
                location=location,
                title_l=title.lower(),
                change_ctx=rng.choice(CHANGE_CONTEXT),
                service=service,
                business_unit=business_unit,
                platform=platform,
                cause1=cause1,
                cause2=cause2,
            ) + " " + rng.choice(USER_NOTES)

            summary = (
                f"Likely causes: {cause1}, {cause2}. "
                f"Recommended checks: {check1}, {check2}. "
                f"Recommended actions: {action1}, {action2}."
            )

            tags = sorted(set([
                scope["domain"],
                class_key,
                severity,
                environment,
                re.sub(r"[^a-z0-9]+", "_", service.lower()).strip("_"),
                "enterprise_canonical",
            ]))

            record = {
                "id": f"inc_{seq}",
                "domain": scope["domain"],
                "service_scope_area": scope_name,
                "classification": class_key,
                "title": title,
                "summary": summary,
                "description": description,
                "symptoms": [symptom1, symptom2],
                "likely_causes": [cause1, cause2],
                "recommended_checks": [check1, check2],
                "recommended_actions": [action1, action2],
                "severity": severity,
                "priority": priority,
                "environment": environment,
                "impacted_service": service,
                "affected_user_scope": impacted_scope,
                "business_unit": business_unit,
                "location": location,
                "platform_hint": platform,
                "source": "canonical",
                "created_at": created_at,
                "recency_bucket": recency_bucket(age_days),
                "staleness_score": compute_staleness_score(age_days),
                "escalation_team": scope["team"],
                "automation_candidate": class_cfg["automation_candidate"],
                "change_related": rng.random() < 0.34,
                "kb_hint": f"{scope['domain']}/{class_key}",
                "resolution_confidence": round(rng.uniform(0.62, 0.96), 3),
                "tags": tags,
            }
            records.append(record)
            seq += 1

    rng.shuffle(records)
    return records, scope_counts


def default_output_path() -> Path:
    if settings is not None and getattr(settings, "INCIDENTS_FILE", None):
        return Path(settings.INCIDENTS_FILE)
    return Path("data/incidents.json")


def main():
    parser = argparse.ArgumentParser(description="Generate a 5,000-record enterprise incident corpus for AI Ops.")
    parser.add_argument("--count", type=int, default=5000, help="Number of incidents to generate.")
    parser.add_argument("--seed", type=int, default=20260412, help="Random seed for reproducibility.")
    parser.add_argument("--output", type=Path, default=default_output_path(), help="Output JSON path.")
    args = parser.parse_args()

    records, scope_counts = generate_records(count=args.count, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)

    print(f"Generated {len(records)} incidents -> {args.output}")
    print("Scope counts:")
    for scope_name, scope_count in scope_counts.items():
        print(f"  - {scope_name}: {scope_count}")


if __name__ == "__main__":
    main()
