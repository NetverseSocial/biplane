/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
// ui
import { useTranslation } from "@plane/i18n";
import { Tooltip } from "@plane/propel/tooltip";
// hooks
import { usePlatformOS } from "@/hooks/use-platform-os";
import packageJson from "package.json";
// local components
import { Button } from "@plane/propel/button";
// biplane: the edition badge opens the biplane about modal (fork identity + Plane credit)
// instead of the upstream paid-plan upsell.
import { BiplaneAboutModal } from "@/app/(all)/[workspaceSlug]/(projects)/biplane-about";
import { BiplaneLogo } from "@/app/(all)/[workspaceSlug]/(projects)/biplane-logo";

export const WorkspaceEditionBadge = observer(function WorkspaceEditionBadge() {
  // states
  const [isAboutOpen, setIsAboutOpen] = useState(false);
  // translation
  const { t } = useTranslation();
  // platform
  const { isMobile } = usePlatformOS();

  return (
    <>
      <BiplaneAboutModal isOpen={isAboutOpen} onClose={() => setIsAboutOpen(false)} />
      <Tooltip tooltipContent={`biplane · built on Plane v${packageJson.version}`} isMobile={isMobile}>
        <Button
          variant="tertiary"
          size="lg"
          onClick={() => setIsAboutOpen(true)}
          aria-haspopup="dialog"
          aria-label={t("aria_labels.projects_sidebar.edition_badge")}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <BiplaneLogo size={15} />
            Community
          </span>
        </Button>
      </Tooltip>
    </>
  );
});
