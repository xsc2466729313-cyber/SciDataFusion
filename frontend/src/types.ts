export type TabKey = "overview" | "sources" | "evidence" | "delivery" | "settings";

export interface PlatformComponent {
  name: string;
  status: "ready" | "optional" | "disabled" | "unavailable";
  detail: string;
}

export interface PlatformStatus {
  mode: "local" | "celery";
  components: PlatformComponent[];
}

export interface ResearchJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  failure_code: string | null;
  result: {
    quality_gate_passed: boolean;
    quality_score: number;
    source_count: number;
    evidence_count: number;
    artifact_count: number;
    issue_count: number;
    formal_gold_record_count: number;
    package_filename: string;
  } | null;
}

export interface WorkbenchStage {
  key: string;
  label: string;
  status: "complete" | "review" | "blocked";
  primary_count: number;
  count_label: string;
  detail: string;
}

export interface WorkbenchSource {
  candidate_id: string;
  rank: number;
  source_names: string[];
  categories: string[];
  covered_fields: string[];
  license_status: string;
  download_status: string;
  primary: boolean;
  score: number;
}

export interface WorkbenchArtifact {
  object_id: string;
  format: string;
  media_type: string;
  size_bytes: number;
  disposition: string;
  parser: string | null;
  confidence: number;
  sha256: string;
}

export interface WorkbenchEvidence {
  evidence_id: string;
  field_name: string;
  raw_value: string;
  source_location: string;
  byte_range: string;
  method: string;
  confidence: number;
  source_hash: string;
}

export interface StructuredColumnProfile {
  name: string;
  column_index: number;
  non_empty_count: number;
  empty_count: number;
  null_count: number;
}

export interface StructuredCellEvidence {
  evidence_id: string;
  row_index: number;
  column_index: number;
  column_name: string;
  raw_value_json: string;
  source_location: string;
  source_hash: string;
}

export interface StructuredDatasetPreview {
  dataset_id: string;
  artifact_sha256: string;
  source_url: string;
  media_type: string;
  format: "csv" | "tsv" | "json";
  parser_id: string;
  parser_version: string;
  row_count: number;
  column_count: number;
  preview_row_count: number;
  preview_column_count: number;
  truncated: boolean;
  columns: StructuredColumnProfile[];
  cells: StructuredCellEvidence[];
  dataset_hash: string;
}

export interface OnlineStructuredDataResult {
  attempted_count: number;
  datasets: StructuredDatasetPreview[];
  failures: Array<{
    artifact_sha256: string;
    code: string;
    detail: string;
  }>;
}

export interface WorkbenchIssue {
  issue_id: string;
  code: string;
  severity: string;
  fields: string[];
  detail: string;
  action: string;
  evidence_count: number;
}

export interface GraphNode {
  node_id: string;
  kind: string;
  source_id: string;
  label: string;
  trusted: boolean;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: string;
  evidence_refs: string[];
}

export interface ResearchBlueprint {
  topic_title: string;
  research_summary: string;
  evidence_priorities: string[];
  source_types: string[];
  candidate_fields: string[];
  target_outputs: string[];
}

export interface WorkbenchSnapshot {
  execution_mode: "offline" | "online";
  research_goal: string;
  retrieval_query: string;
  topic_data_status: "reference_demo" | "live_discovery";
  status: string;
  task_id: string;
  run_id: string;
  quality_score: number;
  quality_gate_passed: boolean;
  stages: WorkbenchStage[];
  sources: WorkbenchSource[];
  artifacts: WorkbenchArtifact[];
  evidence: WorkbenchEvidence[];
  issues: WorkbenchIssue[];
  graph_nodes: GraphNode[];
  graph_edges: GraphEdge[];
  research_blueprint: ResearchBlueprint;
  delivery_artifact_count: number;
  package_filename: string;
  formal_gold_available: boolean;
  online_research: { results: Array<{ title: string; url: string; domain: string; channel: string }> } | null;
  online_structured_data: OnlineStructuredDataResult | null;
}

export interface OnlineConfiguration {
  execution_enabled: boolean;
  online_ready: boolean;
  model_base_url: string | null;
  bailian_region: "cn-beijing" | "us-virginia" | "ap-southeast-1" | "ap-northeast-1";
  planner_model_id: string;
  assessment_model_id: string;
  search_engine: "google" | "google_scholar";
  search_language: string;
  search_country: string | null;
  query_planning_enabled: boolean;
  max_search_queries: number;
  max_search_results: number;
  credentials: Array<{ environment_variable: string; configured: boolean }>;
}
