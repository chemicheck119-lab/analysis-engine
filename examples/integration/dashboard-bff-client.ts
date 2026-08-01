import type {
  ConfirmationRequest,
  ConfirmationResponse,
  DashboardError,
  IncidentAnalysisResponse,
  IncidentAnalyzeRequest,
  MaterialDiscoveryRequest,
  MaterialDiscoveryResponse,
  MovementUpdateRequest,
  MovementUpdateResponse,
  OperationsAgentSnapshot,
  RecordSaveRequest,
  RecordSaveResponse,
} from '../../contracts/dashboard-bff-v1.types';

const DEFAULT_BFF_BASE_URL = '/api/c2guard/v1';

function bffBaseUrl(): string {
  const configured = import.meta.env.VITE_CHEMICHECK119_BFF_BASE_URL;
  return (configured || DEFAULT_BFF_BASE_URL).replace(/\/$/, '');
}

async function postJson<TResponse>(
  path: string,
  body: unknown,
): Promise<TResponse> {
  const response = await fetch(`${bffBaseUrl()}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });

  const payload = (await response.json()) as TResponse | DashboardError;
  if (!response.ok) {
    const failure = payload as DashboardError;
    throw new Error(
      `${failure.error.code}: ${failure.error.message} (requestId=${failure.requestId})`,
    );
  }
  return payload as TResponse;
}

export function discoverMaterials(
  request: MaterialDiscoveryRequest,
): Promise<MaterialDiscoveryResponse> {
  return postJson('/substances/discover', request);
}

export function analyzeIncident(
  request: IncidentAnalyzeRequest,
): Promise<IncidentAnalysisResponse> {
  return postJson('/incidents/analyze', request);
}

export function confirmOnSiteSubstance(
  incidentId: string,
  request: ConfirmationRequest,
): Promise<ConfirmationResponse> {
  return postJson(
    `/incidents/${encodeURIComponent(incidentId)}/confirmations`,
    request,
  );
}

export async function confirmAndRefreshIncident(
  incidentId: string,
  confirmationRequest: ConfirmationRequest,
  analysisRequest: Omit<IncidentAnalyzeRequest, 'incidentId'>,
): Promise<{
  confirmation: ConfirmationResponse;
  analysis: IncidentAnalysisResponse;
}> {
  const confirmation = await confirmOnSiteSubstance(
    incidentId,
    confirmationRequest,
  );
  const analysis = await analyzeIncident({
    ...analysisRequest,
    incidentId,
  });
  return { confirmation, analysis };
}

export function saveIncidentRecord(
  incidentId: string,
  request: RecordSaveRequest,
): Promise<RecordSaveResponse> {
  return postJson(
    `/incidents/${encodeURIComponent(incidentId)}/record`,
    request,
  );
}

export function canDisplayConflictRisk(
  response: IncidentAnalysisResponse,
): response is Extract<
  IncidentAnalysisResponse,
  { state: 'SCREENING_COMPLETED' }
> {
  return (
    response.confirmationGate.allRequiredConfirmed === true &&
    response.confirmationGate.ruleExecutionAllowed === true &&
    response.conflictReview.executed === true &&
    response.conflictReview.riskDisplayAllowed === true &&
    response.riskDisplayAllowed === true
  );
}

export function canRenderProviderRoute(
  agent: OperationsAgentSnapshot | null | undefined,
): agent is OperationsAgentSnapshot & {
  mapContext: {
    route: {
      status: 'AVAILABLE' | 'DEMO_SIMULATION';
      geometry: NonNullable<
        OperationsAgentSnapshot['mapContext']['route']['geometry']
      >;
    };
  };
} {
  return Boolean(
    agent &&
      (agent.mapContext.route.status === 'AVAILABLE' ||
        agent.mapContext.route.status === 'DEMO_SIMULATION') &&
      agent.mapContext.route.geometry,
  );
}

export function updateIncidentMovement(
  incidentId: string,
  request: MovementUpdateRequest,
): Promise<MovementUpdateResponse> {
  return postJson(
    `/incidents/${encodeURIComponent(incidentId)}/movement`,
    request,
  );
}

export async function saveThenReset(
  incidentId: string,
  request: RecordSaveRequest,
  resetScreen: () => void,
): Promise<RecordSaveResponse> {
  const saved = await saveIncidentRecord(incidentId, request);
  if (saved.resetAllowed) {
    resetScreen();
  }
  return saved;
}
