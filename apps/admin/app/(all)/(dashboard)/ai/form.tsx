/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { RefreshCw } from "lucide-react";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { InstanceService } from "@plane/services";
import type { IFormattedInstanceConfiguration, TInstanceAIConfigurationKeys } from "@plane/types";
import { CustomSelect, Input } from "@plane/ui";
// components
import type { TControllerInputFormField } from "@/components/common/controller-input";
import { ControllerInput } from "@/components/common/controller-input";
// hooks
import { useInstance } from "@/hooks/store";

const instanceService = new InstanceService();

type IInstanceAIForm = {
  config: IFormattedInstanceConfiguration;
};

type AIFormValues = Record<TInstanceAIConfigurationKeys, string>;

// biplane: any OpenAI-compatible endpoint works — pick a preset (which fills the base
// URL) or "Custom" to type your own. An operator can pin their own default at the top
// of this list at build time via VITE_LLM_DEFAULT_LABEL + VITE_LLM_DEFAULT_BASE_URL.
type TProviderPreset = { id: string; label: string; provider: string; base: string; editable?: boolean };

const DEFAULT_LABEL = import.meta.env.VITE_LLM_DEFAULT_LABEL as string | undefined;
const DEFAULT_BASE = import.meta.env.VITE_LLM_DEFAULT_BASE_URL as string | undefined;

const PROVIDER_PRESETS: TProviderPreset[] = [
  ...(DEFAULT_LABEL && DEFAULT_BASE
    ? [{ id: "default", label: DEFAULT_LABEL, provider: "custom", base: DEFAULT_BASE }]
    : []),
  { id: "openai", label: "OpenAI", provider: "openai", base: "" },
  { id: "openrouter", label: "OpenRouter", provider: "custom", base: "https://openrouter.ai/api/v1" },
  { id: "together", label: "Together AI", provider: "custom", base: "https://api.together.xyz/v1" },
  { id: "groq", label: "Groq", provider: "custom", base: "https://api.groq.com/openai/v1" },
  { id: "custom", label: "Custom (OpenAI-compatible)", provider: "custom", base: "", editable: true },
];

// Map a saved base URL back to the preset that produced it (else "Custom").
const presetForBase = (base: string): TProviderPreset => {
  if (!base) return PROVIDER_PRESETS.find((p) => p.id === "openai")!;
  return PROVIDER_PRESETS.find((p) => p.base === base && !p.editable) ?? PROVIDER_PRESETS.find((p) => p.editable)!;
};

export function InstanceAIForm(props: IInstanceAIForm) {
  const { config } = props;
  // store
  const { updateInstanceConfigurations } = useInstance();
  // form data
  const {
    handleSubmit,
    control,
    watch,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<AIFormValues>({
    defaultValues: {
      LLM_API_KEY: config["LLM_API_KEY"],
      LLM_MODEL: config["LLM_MODEL"],
      LLM_PROVIDER: config["LLM_PROVIDER"] || "openai",
      LLM_API_BASE: config["LLM_API_BASE"] || "",
    },
  });

  // biplane: selection is explicit state, seeded from the saved base URL. Deriving it
  // from the base on every render collapsed "Custom" (base "") straight back to OpenAI.
  const [presetId, setPresetId] = useState<string>(() => presetForBase(config["LLM_API_BASE"] || "").id);
  const selectedPreset = PROVIDER_PRESETS.find((p) => p.id === presetId) ?? PROVIDER_PRESETS[0];
  const isCustom = Boolean(selectedPreset.editable);

  // biplane: models fetched from the endpoint's /models — lets the admin pick a real
  // model instead of typing one blind. Empty = fall back to the free-text Model field.
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  // A list belongs to ONE endpoint identity. The counter invalidates in-flight
  // responses: switching provider/base bumps it, so a slow response from the old
  // endpoint can never repopulate the new endpoint's dropdown.
  const modelsRequestRef = useRef(0);

  const invalidateModels = () => {
    modelsRequestRef.current += 1;
    setAvailableModels([]);
  };

  // The API key is part of the endpoint identity too, but its field renders via the
  // generic ControllerInput — so watch the value instead of hooking its onChange.
  const apiKeyValue = watch("LLM_API_KEY");
  const prevApiKeyRef = useRef(apiKeyValue);
  useEffect(() => {
    if (prevApiKeyRef.current !== apiKeyValue) {
      prevApiKeyRef.current = apiKeyValue;
      invalidateModels();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiKeyValue]);

  const loadModels = async () => {
    const requestId = ++modelsRequestRef.current;
    setLoadingModels(true);
    try {
      const models = await instanceService.listLLMModels(watch("LLM_API_BASE") || "", watch("LLM_API_KEY") || "");
      if (requestId !== modelsRequestRef.current) return; // endpoint changed mid-flight
      setAvailableModels(models);
      if (models.length === 0) {
        setToast({ type: TOAST_TYPE.INFO, title: "No models", message: "The endpoint returned no models." });
      } else {
        setToast({ type: TOAST_TYPE.SUCCESS, title: "Models loaded", message: `${models.length} model(s) available.` });
      }
    } catch (err: unknown) {
      if (requestId !== modelsRequestRef.current) return;
      const message = (err as { error?: string })?.error || "Could not load models from this endpoint.";
      setToast({ type: TOAST_TYPE.ERROR, title: "Error", message });
    } finally {
      if (requestId === modelsRequestRef.current) setLoadingModels(false);
    }
  };

  const aiFormFields: TControllerInputFormField[] = [
    {
      key: "LLM_API_KEY",
      type: "password",
      label: "API key",
      description: <>The API key for the endpoint above.</>,
      placeholder: "sk-…",
      error: Boolean(errors.LLM_API_KEY),
      required: false,
    },
  ];

  const onSubmit = async (formData: AIFormValues) => {
    // A custom endpoint with no base URL would silently disable AI server-side —
    // block the save and say so instead.
    if (isCustom && !formData.LLM_API_BASE?.trim()) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Base URL required",
        message: "Enter the endpoint's base URL, or pick a preset provider.",
      });
      return;
    }
    const payload: Partial<AIFormValues> = { ...formData };
    await updateInstanceConfigurations(payload)
      .then(() =>
        setToast({
          type: TOAST_TYPE.SUCCESS,
          title: "Success",
          message: "AI Settings updated successfully",
        })
      )
      .catch((err) => console.error(err));
  };

  return (
    <div className="space-y-8">
      <div className="space-y-3">
        <div>
          <div className="pb-1 text-18 font-medium text-primary">AI model provider</div>
          <div className="text-13 font-regular text-tertiary">
            Any OpenAI-compatible endpoint — OpenAI itself, a hosted gateway, or your own server.
          </div>
        </div>

        <div className="grid-col grid w-full grid-cols-1 items-start justify-between gap-x-12 gap-y-8 lg:grid-cols-3">
          {/* Provider preset */}
          <div className="flex flex-col gap-1">
            <h4 className="text-13 text-tertiary">Provider</h4>
            <Controller
              control={control}
              name="LLM_PROVIDER"
              render={({ field: { value, onChange } }) => (
                <CustomSelect
                  value={selectedPreset.id}
                  label={selectedPreset.label}
                  onChange={(nextId: string) => {
                    const preset = PROVIDER_PRESETS.find((p) => p.id === nextId);
                    if (!preset) return;
                    setPresetId(preset.id);
                    onChange(preset.provider);
                    setValue("LLM_API_BASE", preset.base);
                    // A model list belongs to one endpoint — switching endpoints
                    // must not leave the previous endpoint's models selectable,
                    // and must supersede any in-flight fetch.
                    invalidateModels();
                  }}
                  buttonClassName="border-subtle"
                  input
                >
                  {PROVIDER_PRESETS.map((p) => (
                    <CustomSelect.Option key={p.id} value={p.id}>
                      {p.label}
                    </CustomSelect.Option>
                  ))}
                </CustomSelect>
              )}
            />
          </div>

          {/* Base URL — editable only for the Custom preset */}
          <div className="flex flex-col gap-1">
            <h4 className="text-13 text-tertiary">Base URL</h4>
            <Controller
              control={control}
              name="LLM_API_BASE"
              render={({ field: { value, onChange } }) => (
                <Input
                  id="LLM_API_BASE"
                  type="text"
                  value={value}
                  onChange={(e) => {
                    onChange(e.target.value);
                    // Editing the URL changes the endpoint identity — the old list
                    // (and any fetch still in flight) is no longer valid.
                    invalidateModels();
                  }}
                  placeholder="https://api.openai.com/v1"
                  className="w-full border border-subtle !bg-surface-1"
                  disabled={!isCustom}
                />
              )}
            />
            <span className="text-11 text-tertiary">
              {isCustom ? "Enter your OpenAI-compatible endpoint." : "Set by the selected provider."}
            </span>
          </div>

          {/* LLM Model — a dropdown once models are fetched, else free text */}
          <div className="flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <h4 className="text-13 text-tertiary">LLM Model</h4>
              <button
                type="button"
                onClick={loadModels}
                disabled={loadingModels}
                className="flex items-center gap-1 text-11 text-accent-primary hover:underline disabled:opacity-60"
              >
                <RefreshCw className={`size-3 ${loadingModels ? "animate-spin" : ""}`} />
                {loadingModels ? "Loading…" : "Load models"}
              </button>
            </div>
            <Controller
              control={control}
              name="LLM_MODEL"
              render={({ field: { value, onChange } }) =>
                availableModels.length > 0 ? (
                  <CustomSelect
                    value={value}
                    label={value || <span className="text-placeholder">Select a model</span>}
                    onChange={onChange}
                    buttonClassName="border-subtle"
                    input
                  >
                    {availableModels.map((m) => (
                      <CustomSelect.Option key={m} value={m}>
                        {m}
                      </CustomSelect.Option>
                    ))}
                  </CustomSelect>
                ) : (
                  <Input
                    id="LLM_MODEL"
                    type="text"
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder="gpt-4o-mini"
                    className="w-full border border-subtle !bg-surface-1"
                  />
                )
              }
            />
            <span className="text-11 text-tertiary">
              {availableModels.length > 0
                ? "Pick a model from your endpoint."
                : "Type a model name, or Load models to pick from your endpoint."}
            </span>
          </div>

          {aiFormFields.map((field) => (
            <ControllerInput
              key={field.key}
              control={control}
              type={field.type}
              name={field.key}
              label={field.label}
              description={field.description}
              placeholder={field.placeholder}
              error={field.error}
              required={field.required}
            />
          ))}
        </div>
      </div>

      <div className="flex flex-col items-start gap-4">
        <Button variant="primary" size="lg" onClick={handleSubmit(onSubmit)} loading={isSubmitting}>
          {isSubmitting ? "Saving" : "Save changes"}
        </Button>
      </div>
    </div>
  );
}
