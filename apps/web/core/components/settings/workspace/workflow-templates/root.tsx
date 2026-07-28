/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// biplane: manage reusable workflow-state templates. Built-in (system) templates are
// read-only but can be duplicated; workspace templates can be created, edited, and
// deleted. A new project adopts the states of whichever template is chosen at creation.
import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Copy, GitBranchPlus, Plus, Trash2, X } from "lucide-react";
import { Button, CustomSelect, Input, setToast, TOAST_TYPE } from "@plane/ui";
import { WorkspaceService } from "@plane/services";

const workspaceService = new WorkspaceService();

const GROUPS = ["backlog", "unstarted", "started", "completed", "cancelled", "triage"] as const;
const GROUP_COLOR: Record<string, string> = {
  backlog: "#60646C",
  unstarted: "#3b82f6",
  started: "#F59E0B",
  completed: "#46A758",
  cancelled: "#ef4444",
  triage: "#4E5355",
};

type TState = { name: string; group: string; color: string; sequence?: number; default?: boolean };
type TTemplate = { id: string; name: string; description: string; is_system: boolean; states: TState[] };

const blankState = (): TState => ({ name: "", group: "unstarted", color: "#3b82f6" });

export function WorkflowTemplatesRoot({ workspaceSlug }: { workspaceSlug: string }) {
  const [templates, setTemplates] = useState<TTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  // editor draft; null = not editing
  const [draft, setDraft] = useState<TTemplate | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    workspaceService
      .workflowTemplates(workspaceSlug)
      .then((data) => setTemplates(data as TTemplate[]))
      .catch(() => setTemplates([]))
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    if (workspaceSlug) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceSlug]);

  const startNew = () =>
    setDraft({ id: "", name: "", description: "", is_system: false, states: [blankState()] });
  const startDuplicate = (t: TTemplate) =>
    setDraft({
      id: "",
      name: `${t.name} copy`,
      description: t.description,
      is_system: false,
      states: t.states.map((s) => ({ ...s })),
    });
  const startEdit = (t: TTemplate) => setDraft({ ...t, states: t.states.map((s) => ({ ...s })) });

  const setStates = (states: TState[]) => setDraft((d) => (d ? { ...d, states } : d));
  const updateState = (i: number, patch: Partial<TState>) =>
    setStates(draft!.states.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  const moveState = (i: number, dir: -1 | 1) => {
    const arr = [...draft!.states];
    const j = i + dir;
    if (j < 0 || j >= arr.length) return;
    [arr[i], arr[j]] = [arr[j], arr[i]];
    setStates(arr);
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    const payload = { name: draft.name, description: draft.description, states: draft.states };
    try {
      if (draft.id) await workspaceService.updateWorkflowTemplate(workspaceSlug, draft.id, payload);
      else await workspaceService.createWorkflowTemplate(workspaceSlug, payload);
      setToast({ type: TOAST_TYPE.SUCCESS, title: "Saved", message: "Workflow template saved." });
      setDraft(null);
      load();
    } catch (err: unknown) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Error",
        message: (err as { error?: string })?.error || "Could not save the template.",
      });
    } finally {
      setSaving(false);
    }
  };

  const remove = async (t: TTemplate) => {
    try {
      await workspaceService.deleteWorkflowTemplate(workspaceSlug, t.id);
      setToast({ type: TOAST_TYPE.SUCCESS, title: "Deleted", message: `${t.name} removed.` });
      load();
    } catch (err: unknown) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Error",
        message: (err as { error?: string })?.error || "Could not delete the template.",
      });
    }
  };

  if (draft) {
    return (
      <div className="space-y-5">
        <div className="flex items-center justify-between">
          <h3 className="text-16 font-medium text-primary">{draft.id ? "Edit workflow" : "New workflow"}</h3>
          <button onClick={() => setDraft(null)} className="text-tertiary hover:text-primary">
            <X className="size-4" />
          </button>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row">
          <Input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="Workflow name"
            className="w-full border border-subtle sm:w-1/3"
          />
          <Input
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="Short description"
            className="w-full border border-subtle sm:flex-1"
          />
        </div>

        <div className="space-y-2">
          {draft.states.map((s, i) => (
            <div key={i} className="flex items-center gap-2 rounded-md border border-subtle p-2">
              <input
                type="color"
                value={s.color}
                onChange={(e) => updateState(i, { color: e.target.value })}
                className="h-6 w-6 shrink-0 cursor-pointer rounded"
                title="State color"
              />
              <Input
                value={s.name}
                onChange={(e) => updateState(i, { name: e.target.value })}
                placeholder="State name"
                className="flex-1 border border-subtle"
              />
              <div className="w-40 shrink-0">
                <CustomSelect
                  value={s.group}
                  label={s.group}
                  onChange={(g: string) => updateState(i, { group: g, color: s.color || GROUP_COLOR[g] })}
                  buttonClassName="border-subtle capitalize"
                  input
                >
                  {GROUPS.map((g) => (
                    <CustomSelect.Option key={g} value={g}>
                      <span className="capitalize">{g}</span>
                    </CustomSelect.Option>
                  ))}
                </CustomSelect>
              </div>
              <button onClick={() => moveState(i, -1)} className="text-tertiary hover:text-primary" title="Move up">
                <ArrowUp className="size-4" />
              </button>
              <button onClick={() => moveState(i, 1)} className="text-tertiary hover:text-primary" title="Move down">
                <ArrowDown className="size-4" />
              </button>
              <button
                onClick={() => setStates(draft.states.filter((_, idx) => idx !== i))}
                className="text-tertiary hover:text-danger-primary"
                title="Remove state"
                disabled={draft.states.length <= 1}
              >
                <Trash2 className="size-4" />
              </button>
            </div>
          ))}
          <button
            onClick={() => setStates([...draft.states, blankState()])}
            className="flex items-center gap-1 text-13 text-accent-primary hover:underline"
          >
            <Plus className="size-3.5" /> Add state
          </button>
        </div>

        <p className="text-11 text-tertiary">
          A workflow must include at least one state in each of: backlog, unstarted, started, completed, cancelled.
        </p>

        <div className="flex items-center gap-2">
          <Button variant="primary" onClick={save} loading={saving}>
            {saving ? "Saving" : "Save workflow"}
          </Button>
          <Button variant="neutral-primary" onClick={() => setDraft(null)}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-13 text-tertiary">
          Templates a new project can adopt. Built-ins are read-only — duplicate one to customize.
        </p>
        <Button variant="primary" size="sm" prependIcon={<Plus className="size-3.5" />} onClick={startNew}>
          New workflow
        </Button>
      </div>

      {loading ? (
        <p className="text-13 text-tertiary">Loading…</p>
      ) : (
        <div className="divide-y divide-subtle rounded-md border border-subtle">
          {templates.map((t) => (
            <div key={t.id} className="flex items-center gap-3 p-3">
              <GitBranchPlus className="size-4 shrink-0 text-tertiary" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-14 font-medium text-primary">{t.name}</span>
                  {t.is_system && (
                    <span className="rounded bg-layer-1 px-1.5 py-0.5 text-10 text-tertiary">Built-in</span>
                  )}
                </div>
                <p className="truncate text-11 text-tertiary">
                  {t.states.length} states · {t.description}
                </p>
              </div>
              <div className="flex items-center gap-3 text-tertiary">
                <button onClick={() => startDuplicate(t)} className="hover:text-primary" title="Duplicate">
                  <Copy className="size-4" />
                </button>
                {!t.is_system && (
                  <>
                    <button onClick={() => startEdit(t)} className="text-13 hover:text-primary">
                      Edit
                    </button>
                    <button onClick={() => remove(t)} className="hover:text-danger-primary" title="Delete">
                      <Trash2 className="size-4" />
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
