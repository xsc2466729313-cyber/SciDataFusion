import type { OnlineConfiguration, PlatformStatus, ResearchJob, ResearchJobPage, WorkbenchSnapshot } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers }
  });
  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(problem?.detail ?? `请求失败 (${response.status})`);
  }
  return (await response.json()) as T;
}

export const api = {
  platform: () => request<PlatformStatus>("/api/v1/platform"),
  workbench: () => request<WorkbenchSnapshot>("/api/v1/workbench"),
  submitJob: (researchGoal: string, executionMode: "offline" | "online") =>
    request<ResearchJob>("/api/v1/research-jobs", {
      method: "POST",
      body: JSON.stringify({ research_goal: researchGoal, execution_mode: executionMode })
    }),
  job: (jobId: string) => request<ResearchJob>(`/api/v1/research-jobs/${jobId}`),
  jobs: (limit = 1) => request<ResearchJobPage>(`/api/v1/research-jobs?limit=${limit}`),
  configuration: () => request<OnlineConfiguration>("/api/v1/online/configuration"),
  saveConfiguration: (payload: Record<string, unknown>) =>
    request<OnlineConfiguration>("/api/v1/online/configuration", {
      method: "PUT",
      body: JSON.stringify(payload)
    })
};
