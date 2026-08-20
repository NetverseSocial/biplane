/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { API_BASE_URL } from "@plane/constants";
import type {
  IFormattedInstanceConfiguration,
  IInstance,
  IInstanceAdmin,
  IInstanceConfiguration,
  IInstanceInfo,
  TPage,
  TUpdateCheckStatus,
} from "@plane/types";
// api service
import { APIService } from "../api.service";

/**
 * Service class for managing instance-related operations
 * Handles retrieval of instance information and changelog
 * @extends {APIService}
 */
export class InstanceService extends APIService {
  /**
   * Creates an instance of InstanceService
   * Initializes the service with the base API URL
   */
  constructor() {
    super(API_BASE_URL);
  }

  /**
   * Retrieves information about the current instance
   * @returns {Promise<IInstanceInfo>} Promise resolving to instance information
   * @throws {Error} If the API request fails
   * @remarks This method uses the validateStatus: null option to bypass interceptors for unauthorized errors.
   */
  async info(): Promise<IInstanceInfo> {
    return this.get("/api/instances/", { validateStatus: null })
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Fetches the changelog for the current instance
   * @returns {Promise<TPage>} Promise resolving to the changelog page data
   * @throws {Error} If the API request fails
   */
  async changelog(): Promise<TPage> {
    return this.get("/api/instances/changelog/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Retrieves the M5.2 update-check status (instance admin only).
   * Read-only and server-local — never triggers the outbound check itself.
   * @returns {Promise<TUpdateCheckStatus>} the last stored classification
   */
  async updateCheckStatus(): Promise<TUpdateCheckStatus> {
    return this.get("/api/instances/updates/status/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Asks the host applier to apply the flagged update (instance admin only).
   * No tag parameter on purpose: the server sends the update check's flagged
   * tag — the client is not an authority on what should run.
   * @returns the applier's own verdict, passed through verbatim
   */
  async applyUpdate(): Promise<{ started?: string; error?: string }> {
    return this.post("/api/instances/updates/apply/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /** The automatic-updates switch (instance admin only). */
  async autoApplySetting(): Promise<{ enabled: boolean; env_forced: boolean }> {
    return this.get("/api/instances/updates/auto/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async setAutoApplySetting(enabled: boolean): Promise<{ enabled: boolean; env_forced: boolean }> {
    return this.patch("/api/instances/updates/auto/", { enabled })
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /** The update-server preference (instance admin only). */
  async updateSourceSetting(): Promise<{ source: string; custom_url: string | null }> {
    return this.get("/api/instances/updates/source/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async setUpdateSourceSetting(source: string, customUrl: string | null): Promise<{ source: string; custom_url: string | null }> {
    return this.patch("/api/instances/updates/source/", { source, custom_url: customUrl })
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /** Biplane's own changelog, shipped in the backend image. */
  async ourChangelog(): Promise<{ markdown: string | null; error?: string }> {
    return this.get("/api/instances/updates/changelog/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * The applier's run status (instance admin only), passed through verbatim.
   */
  async applyUpdateStatus(): Promise<{
    running: boolean;
    last_result: { tag: string; exit_code: number; finished_at: number } | null;
    log_tail: string;
  }> {
    return this.get("/api/instances/updates/apply/status/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Fetches the list of instance admins
   * @returns {Promise<IInstanceAdmin[]>} Promise resolving to an array of instance admins
   * @throws {Error} If the API request fails
   * @remarks This method uses the validateStatus: null option to bypass interceptors for unauthorized errors.
   */
  async admins(): Promise<IInstanceAdmin[]> {
    return this.get("/api/instances/admins/", { validateStatus: null })
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Updates the instance information
   * @param {Partial<IInstance>} data Data to update the instance with
   * @returns {Promise<IInstance>} Promise resolving to the updated instance information
   * @throws {Error} If the API request fails
   */
  async update(data: Partial<IInstance>): Promise<IInstance> {
    return this.patch("/api/instances/", data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Fetches the list of instance configurations
   * @returns {Promise<IInstanceConfiguration[]>} Promise resolving to an array of instance configurations
   * @throws {Error} If the API request fails
   */
  async configurations(): Promise<IInstanceConfiguration[]> {
    return this.get("/api/instances/configurations/")
      .then((response) => response.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Updates the instance configurations
   * @param {Partial<IFormattedInstanceConfiguration>} data Data to update the instance configurations with
   * @returns {Promise<IInstanceConfiguration[]>} The updated instance configurations
   * @throws {Error} If the API request fails
   */
  async updateConfigurations(data: Partial<IFormattedInstanceConfiguration>): Promise<IInstanceConfiguration[]> {
    return this.patch("/api/instances/configurations/", data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Sends a test email to the specified receiver to test SMTP configuration
   * @param {string} receiverEmail Email address to send the test email to
   * @returns {Promise<void>} Promise resolving to void
   * @throws {Error} If the API request fails
   */
  async sendTestEmail(receiverEmail: string): Promise<void> {
    return this.post("/api/instances/email-credentials-check/", {
      receiver_email: receiverEmail,
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * biplane: list models available at an OpenAI-compatible endpoint.
   * Pass the base URL + key the admin just typed (unsaved), or omit to use saved config.
   * @returns {Promise<string[]>} available model ids
   * @throws {Error} If the API request fails
   */
  async listLLMModels(apiBase?: string, apiKey?: string): Promise<string[]> {
    return this.post("/api/instances/llm-models/", {
      ...(apiBase !== undefined ? { api_base: apiBase } : {}),
      ...(apiKey ? { api_key: apiKey } : {}),
    })
      .then((response) => response?.data?.models ?? [])
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  /**
   * Disables the email configuration
   * @returns {Promise<void>} Promise resolving to void
   * @throws {Error} If the API request fails
   */
  async disableEmail(): Promise<void> {
    return this.delete("/api/instances/configurations/disable-email-feature/")
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
