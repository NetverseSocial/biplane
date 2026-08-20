# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import os

# Module imports
from plane.authentication.adapter.credential import CredentialAdapter
from plane.authentication.adapter.error import (
    AUTHENTICATION_ERROR_CODES,
    AuthenticationException,
)
from plane.db.models import User
from plane.license.utils.instance_value import get_configuration_value
from plane.utils.name_policy import normalize_name


class EmailProvider(CredentialAdapter):
    provider = "email"

    def __init__(
        self,
        request,
        key=None,
        code=None,
        is_signup=False,
        callback=None,
        first_name="",
        last_name="",
        accept_weak_password=False,
    ):
        super().__init__(request=request, provider=self.provider, callback=callback)
        self.key = key
        self.code = code
        self.is_signup = is_signup
        # biplane (BIP-21, Morrow RC 3092): normalize_name, NOT str.strip().
        #
        # The old comment claimed values arriving here were "storable as-is"
        # because the endpoint had validated them. That was the bug. The
        # endpoint validated a STRIPPED form and passed the RAW POST value
        # here, and `str.strip()` does not remove U+FEFF — so a byte-order mark
        # survived into the stored name, and the client then refused to edit
        # it. Different strip sets on the two sides of one write path.
        #
        # This now applies the same policy the endpoint validated against, so
        # the stored value is the validated value even if a future caller
        # forgets to normalise. Idempotent, so double-normalising is free.
        self.first_name = normalize_name(first_name)
        self.last_name = normalize_name(last_name)
        # biplane: strength is a warning, not a wall — an explicit user override
        # (checkbox on the form) skips the zxcvbn gate, mirroring instance setup.
        self.accept_weak_password = bool(accept_weak_password)

        (ENABLE_EMAIL_PASSWORD,) = get_configuration_value([
            {
                "key": "ENABLE_EMAIL_PASSWORD",
                "default": os.environ.get("ENABLE_EMAIL_PASSWORD"),
            }
        ])

        if ENABLE_EMAIL_PASSWORD == "0":
            raise AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["EMAIL_PASSWORD_AUTHENTICATION_DISABLED"],
                error_message="EMAIL_PASSWORD_AUTHENTICATION_DISABLED",
            )

    def set_user_data(self):
        if self.is_signup:
            # Check if the user already exists
            if User.objects.filter(email=self.key).exists():
                self.logger.warning("User already exists")
                raise AuthenticationException(
                    error_message="USER_ALREADY_EXIST",
                    error_code=AUTHENTICATION_ERROR_CODES["USER_ALREADY_EXIST"],
                )

            super().set_user_data({
                "email": self.key,
                "user": {
                    "avatar": "",
                    "first_name": self.first_name,
                    "last_name": self.last_name,
                    "provider_id": "",
                    "is_password_autoset": False,
                },
            })
            return
        else:
            user = User.objects.filter(email=self.key).first()

            # User does not exists
            if not user:
                self.logger.warning("User does not exist")
                raise AuthenticationException(
                    error_message="USER_DOES_NOT_EXIST",
                    error_code=AUTHENTICATION_ERROR_CODES["USER_DOES_NOT_EXIST"],
                    payload={"email": self.key},
                )

            # Check user password
            if not user.check_password(self.code):
                self.logger.warning("Authentication failed - invalid credentials")
                raise AuthenticationException(
                    error_message=(
                        "AUTHENTICATION_FAILED_SIGN_UP" if self.is_signup else "AUTHENTICATION_FAILED_SIGN_IN"
                    ),
                    error_code=AUTHENTICATION_ERROR_CODES[
                        ("AUTHENTICATION_FAILED_SIGN_UP" if self.is_signup else "AUTHENTICATION_FAILED_SIGN_IN")
                    ],
                    payload={"email": self.key},
                )

            super().set_user_data({
                "email": self.key,
                "user": {
                    "avatar": "",
                    "first_name": "",
                    "last_name": "",
                    "provider_id": "",
                    "is_password_autoset": False,
                },
            })
            return
