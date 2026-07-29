/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import Link from "next/link";
import { BiplaneLogo } from "@/components/common/biplane-logo";

export function AuthHeader() {
  return (
    <div className="sticky top-0 flex w-full flex-shrink-0 items-center justify-between gap-6">
      <Link href="/">
        <span className="flex items-center gap-3 text-primary">
          <BiplaneLogo size={56} />
          <span className="text-[30px] font-semibold tracking-tight">Biplane</span>
        </span>
      </Link>
    </div>
  );
}
