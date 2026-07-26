/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// ui
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
// components
import { ProductUpdatesFooter } from "@/components/global";
// plane web components
import { ProductUpdatesChangelog } from "@/plane-web/components/global/product-updates/changelog";
import { ProductUpdatesHeader } from "@/plane-web/components/global/product-updates/header";

export type ProductUpdatesModalProps = {
  isOpen: boolean;
  handleClose: () => void;
};

export const ProductUpdatesModal = observer(function ProductUpdatesModal(props: ProductUpdatesModalProps) {
  const { isOpen, handleClose } = props;

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose} position={EModalPosition.CENTER} width={EModalWidth.XXXXL}>
      <ProductUpdatesHeader />
      <div className="flex flex-col items-center justify-center gap-3 py-16">
            <p className="text-16 font-medium">What&apos;s new in Biplane</p>
            <p className="text-13 text-tertiary">Release notes are published on GitHub.</p>
            <a href="https://github.com/NetverseSocial/biplane/releases" target="_blank" rel="noopener noreferrer" className="text-13 font-medium text-accent-primary underline">Open the Biplane releases page</a>
          </div>
      <ProductUpdatesFooter />
    </ModalCore>
  );
});
