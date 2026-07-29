/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: pick the workflow (set of states) a new project starts with. Options are the
// shared system templates plus this workspace's own. Selecting one sends its id with the
// create request; the backend applies that template's states instead of the defaults.
import { useEffect, useState } from "react";
import { Controller, useFormContext } from "react-hook-form";
import { GitBranchPlus } from "lucide-react";
import { CustomSelect } from "@plane/ui";
import { WorkspaceService } from "@plane/services";

const workspaceService = new WorkspaceService();

type TWorkflowTemplate = {
  id: string;
  name: string;
  description: string;
  is_system: boolean;
  states: { name: string; group: string; color: string }[];
};

export function WorkflowTemplateSelect({ workspaceSlug }: { workspaceSlug: string }) {
  const { control } = useFormContext();
  const [templates, setTemplates] = useState<TWorkflowTemplate[]>([]);

  useEffect(() => {
    let active = true;
    if (!workspaceSlug) return;
    workspaceService
      .workflowTemplates(workspaceSlug)
      .then((data) => {
        if (active) setTemplates(data as TWorkflowTemplate[]);
      })
      .catch(() => {
        if (active) setTemplates([]);
      });
    return () => {
      active = false;
    };
  }, [workspaceSlug]);

  if (templates.length === 0) return null;

  return (
    <Controller
      name="workflow_template_id"
      control={control}
      render={({ field: { onChange, value } }) => {
        const selected = templates.find((t) => t.id === value);
        return (
          <div className="h-7 flex-shrink-0">
            <CustomSelect
              value={value}
              onChange={onChange}
              label={
                <div className="flex h-full items-center gap-1 text-13">
                  <GitBranchPlus className="h-3.5 w-3.5" />
                  {selected ? selected.name : <span className="text-placeholder">Workflow</span>}
                </div>
              }
              placement="bottom-start"
              className="h-full"
              buttonClassName="h-full border-[0.5px] border-subtle"
              noChevron
            >
              {templates.map((t) => (
                <CustomSelect.Option key={t.id} value={t.id}>
                  <div className="flex items-start gap-2">
                    <GitBranchPlus className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                    <div className="-mt-0.5">
                      <p>{t.name}</p>
                      <p className="text-11 text-placeholder">
                        {t.states.length} states · {t.description}
                      </p>
                    </div>
                  </div>
                </CustomSelect.Option>
              ))}
            </CustomSelect>
          </div>
        );
      }}
    />
  );
}
