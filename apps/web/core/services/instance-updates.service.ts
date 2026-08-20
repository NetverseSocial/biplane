/**
 * Copyright (c) 2026 The Biplane Authors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane — the Settings → Updates page's server calls, through the WEB app's
// authenticated client. The shared @plane/services client reaches the API
// with no session attached in this app (measured: user_id null on every call
// while workspace calls from the same page authenticate), so the page used
// it and locked its own admin out. Same pattern as every working service.
import { API_BASE_URL } from "@plane/constants";
import type { TUpdateCheckStatus } from "@plane/types";
import { APIService } from "./api.service";

export class InstanceUpdatesService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async updateCheckStatus(): Promise<TUpdateCheckStatus> {
    return this.get("/api/instances/updates/status/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async applyUpdate(): Promise<{ started?: string; error?: string }> {
    return this.post("/api/instances/updates/apply/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async applyUpdateStatus(): Promise<{
    running: boolean;
    last_result: { tag: string; exit_code: number; finished_at: number } | null;
    log_tail: string;
  }> {
    return this.get("/api/instances/updates/apply/status/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async autoApplySetting(): Promise<{ enabled: boolean; env_forced: boolean }> {
    return this.get("/api/instances/updates/auto/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async setAutoApplySetting(enabled: boolean): Promise<{ enabled: boolean; env_forced: boolean }> {
    return this.patch("/api/instances/updates/auto/", { enabled }).then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async updateSourceSetting(): Promise<{ source: string; custom_url: string | null }> {
    return this.get("/api/instances/updates/source/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async setUpdateSourceSetting(source: string, customUrl: string | null): Promise<{ source: string; custom_url: string | null }> {
    return this.patch("/api/instances/updates/source/", { source, custom_url: customUrl }).then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async ourChangelog(): Promise<{ markdown: string | null; error?: string }> {
    return this.get("/api/instances/updates/changelog/").then((r) => r.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
