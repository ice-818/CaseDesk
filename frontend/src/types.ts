export type Strategy = "direct" | "single" | "multi" | "agent" | "refuse";
export type SessionChannel = "platform" | "shop";
export type PolicyLayer = "platform" | "category" | "merchant";

/** Persisted session scope. Existing reception HTTP responses omit these fields. */
export interface SessionScope {
  channel: SessionChannel;
  merchant_id: string | null;
}

export interface ProductResource {
  sku: string;
  merchant_id: string | null;
  name: string;
  category: string;
  price_cents: number;
  inventory: number;
  tags: string[];
  description: string;
}

export interface ShoppingViewProduct {
  sku: string;
  name: string;
  category: string;
  price_cents: number | null;
  inventory: number | null;
  tags: string[];
  position: number | null;
  feature_scores: Record<string, number>;
  total_score: number | null;
}

export interface ShoppingView {
  view: "list" | "compare" | "select";
  display_index: number;
  stop_reason: string | null;
  notice: string | null;
  selected_sku: string | null;
  compare_skus: string[];
  conditions: {
    max_price_cents: number | null;
    color: string | null;
    size: string | null;
    evidence_tags: string[];
  };
  products: ShoppingViewProduct[];
}

export interface OrderResource {
  order_id: string;
  merchant_id: string;
  user_id: string;
  status: "created" | "paid" | "shipped" | "delivered" | "cancelled" | "refunding" | "refunded";
  items: { sku: string; name: string; quantity: number; unit_price_cents: number; customized: boolean }[];
  total_cents: number;
  created_at: string;
  paid_at: string | null;
  shipped_at: string | null;
  delivered_at: string | null;
  tracking_no: string | null;
  receiver_city: string | null;
}

export interface ReceptionFeedback {
  score: number;
  resolved: boolean;
  reason: string;
  last_assistant_message_id: number | null;
}

export interface Reception {
  session_id: string;
  status: "active" | "closed";
  end_reason: "resolved" | "user_closed" | "timeout" | null;
  resolved: boolean;
  feedback: ReceptionFeedback | null;
}

export interface Citation {
  policy_layer?: PolicyLayer | null;
  merchant_id?: string | null;
  applicable_category?: string | null;
  policy_version?: string | null;
  document_id: string;
  title: string;
  chunk_id: string;
  section: string | null;
}

export interface ActionProposal {
  action_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  summary: string;
}

export interface ChatResponse {
  checkpoint_id?: string | null;
  shopping_view?: ShoppingView | null;
  answer: string;
  session_id: string;
  trace_id: string;
  intent: string | null;
  complexity: string | null;
  strategy: Strategy | null;
  citations: Citation[];
  pending_action: ActionProposal | null;
  refused: boolean;
  error_code: string | null;
  latency: { total_ms: number };
}

export interface ChatRequest {
  message: string;
  session_id?: string;
  confirm_action_id?: string;
}

export interface Capabilities {
  database: string;
  cache: string;
  vector_store: string;
  retriever: string;
  reranker: string;
  llm: string;
  degraded: boolean;
}

export type StreamEvent =
  | { event: "started"; trace_id: string; session_id?: string; checkpoint_id?: string | null }
  | { event: "node"; node: string; intent?: string; strategy?: Strategy }
  | { event: "token"; text: string }
  | { event: "answer"; data: ChatResponse }
  | { event: "done"; trace_id: string }
  | { event: "error"; status_code?: number; error_code?: string; message?: string; trace_id?: string };

export interface AuthUser {
  user_id: string;
  role: "buyer" | "service_agent" | "operator";
  merchant_id: string | null;
  status: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  expires_in: number;
  user: AuthUser;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  state: "complete" | "streaming" | "error" | "stopped";
  citations?: Citation[];
  pendingAction?: ActionProposal | null;
  shoppingView?: ShoppingView | null;
  strategy?: Strategy | null;
  traceId?: string;
  latencyMs?: number;
}

export interface Conversation {
  id: string;
  title: string;
  updatedAt: number;
  sessionId?: string;
  messages: Message[];
}

// ---- Self-evolution human review workbench ----

export type EvolutionStatus =
  | "collect"
  | "attribute"
  | "regress"
  | "gate"
  | "review"
  | "promoted"
  | "blocked"
  | "rejected";

export interface EvolutionTurn {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface FeedbackCase {
  case_id: string;
  session_id: string;
  user_id: string;
  merchant_id: string | null;
  channel: "platform" | "shop";
  score: number;
  feedback_reason: string;
  turns: EvolutionTurn[];
}

export interface FeedbackCasesResponse {
  dataset_id: string;
  name: string;
  version: string;
  cases: FeedbackCase[];
}

export interface AttributedCase {
  case_id: string;
  attribution: string;
  matched_signal: string;
}

export interface AttributionReport {
  items: AttributedCase[];
  counts: Record<string, number>;
}

export interface IntentRegressionCase {
  case_id: string;
  query: string;
  expected_intent: string;
}

export interface GateResult {
  passed: boolean;
  baseline_pass_rate: number;
  candidate_pass_rate: number;
  pass_rate_delta: number;
  improved: string[];
  regressed: string[];
  reasons: string[];
}

export interface EvolutionProposal {
  proposal_id: string;
  title: string;
  status: EvolutionStatus;
  candidate_label: string;
  source_dataset_id: string;
  baseline_run_id: string | null;
  candidate_run_id: string | null;
  attribution: AttributionReport | null;
  gate: GateResult | null;
  review_decision: "approved" | "rejected" | null;
  reviewer: string | null;
  review_note: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface RegressionDataset {
  dataset_id: string;
  name: string;
  version: string;
  task: string;
  status: string;
  reviewer: string | null;
  review_note: string | null;
  payload: {
    name: string;
    version: string;
    cases: IntentRegressionCase[];
  };
}
