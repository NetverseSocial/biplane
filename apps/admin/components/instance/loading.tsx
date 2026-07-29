/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: same Biplane loading treatment as the common spinner — no Plane gif assets.
import { LogoSpinner } from "@/components/common/logo-spinner";

export function InstanceLoading() {
  return (
    <div className="flex items-center justify-center">
      <LogoSpinner />
    </div>
  );
}
