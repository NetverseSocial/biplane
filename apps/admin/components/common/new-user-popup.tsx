/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import Link from "next/link";
// ui
import { Button, getButtonStyling } from "@plane/propel/button";
// hooks
import { BiplaneLogo } from "@/components/common/biplane-logo";
import { useTheme } from "@/hooks/store";

export const NewUserPopup = observer(function NewUserPopup() {
  // hooks
  const { isNewUserPopup, toggleNewUserPopup } = useTheme();

  if (!isNewUserPopup) return <></>;
  return (
    <div className="shadow-md absolute right-8 bottom-8 w-96 rounded-lg border border-subtle bg-surface-1 p-6">
      <div className="flex gap-4">
        <div className="grow">
          <div className="text-14 font-semibold">Create workspace</div>
          <div className="py-2 text-13 font-medium text-tertiary">
            Instance setup done! Welcome to the Biplane instance portal. Start your journey by creating your first
            workspace.
          </div>
          <div className="flex items-center gap-4 pt-2">
            <Link href="/workspace/create" className={getButtonStyling("primary", "lg")}>
              Create workspace
            </Link>
            <Button variant="secondary" size="lg" onClick={toggleNewUserPopup}>
              Close
            </Button>
          </div>
        </div>
        <div className="flex shrink-0 items-center justify-center text-primary">
          <BiplaneLogo size={72} />
        </div>
      </div>
    </div>
  );
});
