# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from .instance import InstanceEndpoint, SignUpScreenVisitedEndpoint


from .configuration import (
    LLMModelsEndpoint,
    EmailCredentialCheckEndpoint,
    InstanceConfigurationEndpoint,
    DisableEmailFeatureEndpoint,
)


from .admin import (
    InstanceAdminEndpoint,
    InstanceAdminSignInEndpoint,
    InstanceAdminSignUpEndpoint,
    InstanceAdminUserMeEndpoint,
    InstanceAdminSignOutEndpoint,
    InstanceAdminUserSessionEndpoint,
)


from .workspace import (
    InstanceWorkSpaceAvailabilityCheckEndpoint,
    InstanceWorkSpaceEndpoint,
)
from .update_check import UpdateCheckStatusEndpoint
from .apply_update import ApplyUpdateEndpoint, ApplyStatusEndpoint
from .auto_apply_setting import AutoApplySettingEndpoint, OurChangelogEndpoint, UpdateSourceSettingEndpoint
