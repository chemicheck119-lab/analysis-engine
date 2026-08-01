/**
 * 케미체크119 대시보드 BFF v1 참조 타입.
 *
 * 단일 원본은 dashboard-bff-v1.openapi.json이다. FE_Repository는 이 파일을
 * 복사해 임의 수정하지 말고 OpenAPI에서 생성하거나 버전을 고정해 사용한다.
 * 브라우저는 서비스 BE만 호출하며 모델 API의 X-API-Key를 보유하지 않는다.
 */

export type DashboardSchemaVersion = 'chemicheck119-dashboard-bff-v1';
export type ModelSchemaVersion = 'chemiguard119-api-v1';

export interface DashboardError {
  schemaVersion: DashboardSchemaVersion;
  requestId: string;
  error: {
    code: string;
    message: string;
    retryable: boolean;
  };
  resetAllowed: false;
}

export interface MaterialDiscoveryRequest {
  query: string;
  topK?: number;
  evidenceTopK?: number;
}

export interface EvidenceCard {
  evidenceId: string;
  casNumber: string;
  source: 'KOSHA' | 'CAMEO';
  title: string;
  bodyLabel: '공식 문서 발췌';
  bodyPreview: string;
  sourceUrl: string;
  documentVersion: string;
  casLinkStatus?: string | null;
}

export interface GroundedRagSummary {
  schemaVersion: 'chemicheck119-grounded-rag-v1';
  status:
    | 'COMPLETED'
    | 'FALLBACK_EXTRACTIVE'
    | 'DISABLED'
    | 'NO_GROUNDED_EVIDENCE'
    | 'NOT_RUN_REQUIRES_CONFIRMED_PAIR'
    | 'NOT_RUN_RULE_NOT_COMPLETED';
  usedLlm: boolean;
  model?: string | null;
  statements: Array<{
    text: string;
    sourceIds: string[];
  }>;
  citations: Array<{
    sourceId: string;
    sourceType: 'KOSHA' | 'CAMEO' | 'CAMEO_RULE_ENGINE';
    title: string;
    casNumber?: string | null;
    sourceUrls: string[];
  }>;
  riskDecisionSource: 'DETERMINISTIC_CAMEO_RULE_ENGINE';
  semanticGroundingVerified: false;
  fallbackReason?: string | null;
  limitations: string[];
}

export interface MaterialCandidate {
  rank: number;
  casNumber: string;
  displayName: string;
  matchBasis:
    | 'IDENTITY_EXPRESSION'
    | 'PUBLIC_PROPERTY_PROFILE'
    | 'IDENTITY_AND_PUBLIC_PROPERTY_PROFILE';
  matchedExpression?: string | null;
  matchedProperties: Array<{
    field: 'physical_state' | 'color' | 'odor' | 'use_description';
    label: string;
    value: string;
  }>;
  propertySource?: {
    label: '소방청 울산 화학물질 정보 기반 관찰 후보';
    sourceId: 'NFA_ULSAN_CHEMICAL_INFORMATION';
    sourceUrl: string;
    documentVersion: string;
  } | null;
  evidenceStatus: string;
  evidenceWarning?: string | null;
  evidenceNotice?: string | null;
  casLinkWarning?: string | null;
  evidenceCards: EvidenceCard[];
  requiresResponderConfirmation: true;
  ruleEligible: false;
  riskDeterminationAllowed: false;
}

export interface MaterialDiscoveryResponse {
  schemaVersion: DashboardSchemaVersion;
  sourceModelSchemaVersion: ModelSchemaVersion;
  requestId: string;
  query: string;
  status:
    | 'CANDIDATES_FOUND'
    | 'NO_RELIABLE_CANDIDATE'
    | 'PROFILE_INDEX_NOT_AVAILABLE';
  searchMode:
    | 'IDENTITY_AND_PROPERTY_RETRIEVAL'
    | 'IDENTITY_RETRIEVAL'
    | 'PROPERTY_PROFILE_RETRIEVAL'
    | 'ABSTAINED';
  candidates: MaterialCandidate[];
  requiresResponderConfirmation: true;
  candidateScoreIsProbability: false;
  riskDisplayAllowed: false;
  noReliableCandidateMeansAbsent: false;
  noReliableCandidateMeansSafe: false;
  notice: string;
  safetyNotice: string;
}

export interface OperationsContext {
  dispatchStationName?: string | null;
  responderPosition?: {
    latitude: number;
    longitude: number;
    observedAt: string;
    source:
      | 'VEHICLE_GPS'
      | 'MDT_DEVICE_GPS'
      | 'MANUAL_DISPATCH'
      | 'DEMO_SIMULATION';
    accuracyM?: number | null;
  } | null;
  /** 길찾기 Secret은 브라우저가 아니라 BE만 보유한다. */
  route?: {
    provider: string;
    mode: 'LIVE_API' | 'CACHED_API' | 'DEMO_SIMULATION';
    routeId: string;
    geometry: {
      type: 'LineString';
      /** GeoJSON 순서: [longitude, latitude] */
      coordinates: Array<[number, number]>;
    };
    distanceM: number;
    durationSeconds: number;
    remainingDistanceM: number;
    remainingDurationSeconds: number;
    generatedAt: string;
    trafficApplied: boolean;
    attribution: string;
    providerReference?: string | null;
  } | null;
  journeyState?: 'DISPATCHED' | 'EN_ROUTE' | 'ARRIVED' | 'ON_SCENE';
}

export interface IncidentAnalyzeRequest {
  incidentId?: string | null;
  text: string;
  inputType?:
    | 'MANUAL_TEXT'
    | 'DISPATCH_TEXT'
    | 'VOICE_TRANSCRIPT'
    | 'STRUCTURED_FORM';
  occurredAt?: string | null;
  location?: {
    facilityName?: string | null;
    address?: string | null;
    province?: string | null;
    latitude?: number | null;
    longitude?: number | null;
    coordinateSource?:
      | 'DISPATCH_SYSTEM'
      | 'GEOCODING_PROVIDER'
      | 'RESPONDER_OBSERVATION'
      | 'MANUAL_ENTRY'
      | 'DEMO_FIXTURE'
      | null;
    geocodingProvider?: string | null;
    resolvedAt?: string | null;
  } | null;
  operationsContext?: OperationsContext | null;
  plannedActions?: string[];
  evidenceTopK?: number;
}

export type AnalysisState =
  | 'AWAITING_SUBSTANCE_CONFIRMATION'
  | 'AWAITING_INCIDENT_CONFIRMATION'
  | 'AWAITING_FACILITY_CONFIRMATION'
  | 'SCREENING_COMPLETED'
  | 'VERIFY_REQUIRED'
  | 'UNCLASSIFIED'
  | 'CAMEO_GROUP_SCREENING_ONLY';

export interface OperationsAgentSnapshot {
  schemaVersion: 'chemicheck119-operations-agent-v1';
  agentType: 'DETERMINISTIC_FIELD_RESPONSE_ORCHESTRATOR';
  phase:
    | 'INCIDENT_INTAKE'
    | 'EN_ROUTE_TRIAGE'
    | 'ON_SCENE_CONFIRMATION'
    | 'CONFLICT_SCREENING_COMPLETE'
    | 'EVIDENCE_REVIEW_REQUIRED';
  currentObjective: string;
  nextActions: string[];
  workflow: Array<{
    stepId:
      | 'INCIDENT_INGESTION'
      | 'INCIDENT_PARSING'
      | 'INCIDENT_LOCATION'
      | 'SUBSTANCE_RESOLUTION'
      | 'FACILITY_HISTORY'
      | 'EVIDENCE_RETRIEVAL'
      | 'ON_SITE_CONFIRMATION'
      | 'CONFLICT_SCREENING'
      | 'GROUNDED_EXPLANATION'
      | 'RESPONSE_RECORD';
    label: string;
    status:
      | 'COMPLETED'
      | 'IN_PROGRESS'
      | 'WAITING'
      | 'BLOCKED'
      | 'NOT_APPLICABLE';
    detail: string;
  }>;
  toolExecutions: Array<{
    toolId:
      | 'RULE_PARSER'
      | 'SUBSTANCE_RESOLVER'
      | 'FACILITY_HISTORY_SEARCH'
      | 'HYBRID_EVIDENCE_RETRIEVER'
      | 'CONFIRMATION_GATE'
      | 'CAMEO_RULE_ENGINE'
      | 'GROUNDED_RAG'
      | 'SERVER_ROUTE_PROVIDER';
    status:
      | 'COMPLETED'
      | 'WAITING'
      | 'BLOCKED'
      | 'FALLBACK'
      | 'NOT_RUN'
      | 'UNAVAILABLE';
    outputReference: string;
    summary: string;
  }>;
  mapContext: {
    coverageScope: 'NATIONWIDE_KOREA';
    incidentPosition?: {
      latitude: number;
      longitude: number;
      label: string;
      source: string;
      observedAt?: string | null;
      accuracyM?: number | null;
      isSimulation: boolean;
    } | null;
    responderPosition?: {
      latitude: number;
      longitude: number;
      label: string;
      source: string;
      observedAt?: string | null;
      accuracyM?: number | null;
      isSimulation: boolean;
    } | null;
    route: {
      status:
        | 'AVAILABLE'
        | 'DEMO_SIMULATION'
        | 'ROUTE_UNAVAILABLE'
        | 'INCIDENT_LOCATION_REQUIRED'
        | 'RESPONDER_POSITION_REQUIRED'
        | 'POSITION_STALE'
        | 'ROUTE_ENDPOINT_MISMATCH'
        | 'ARRIVED';
      provider?: string | null;
      providerMode?: 'LIVE_API' | 'CACHED_API' | 'DEMO_SIMULATION' | null;
      routeId?: string | null;
      geometry?: {
        type: 'LineString';
        coordinates: Array<[number, number]>;
      } | null;
      totalDistanceM?: number | null;
      remainingDistanceM?: number | null;
      etaSeconds?: number | null;
      /** 이동 진행률이며 사고·위험 확률이 아니다. */
      progressRatio?: number | null;
      progressRatioIsProbability: false;
      trafficApplied?: boolean | null;
      generatedAt?: string | null;
      attribution?: string | null;
      message: string;
    };
    rendering: {
      geometryFormat: 'GEOJSON_RFC7946';
      recommendedRenderer: 'MAPLIBRE_GL_JS';
      tileProviderRequired: true;
      attributionRequired: true;
      publicOsmStandardTilesForProduction: false;
      routeAnimationSupported: true;
    };
    hazardOverlayStatus: 'NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL';
  };
  autonomousRiskDecisionAllowed: false;
  finalDecisionAuthority: '현장 지휘관';
  traceIsChainOfThought: false;
}

export interface MovementUpdateRequest {
  responderPosition: NonNullable<OperationsContext['responderPosition']>;
  journeyState: 'DISPATCHED' | 'EN_ROUTE' | 'ARRIVED' | 'ON_SCENE';
  clientSequence: number;
}

export interface MovementUpdateResponse {
  schemaVersion: DashboardSchemaVersion;
  requestId: string;
  incidentId: string;
  acceptedAt: string;
  clientSequence: number;
  mapContext: OperationsAgentSnapshot['mapContext'];
  nextRefreshSeconds: number;
  routeRecalculated: boolean;
}

export interface AnalysisCommon {
  schemaVersion: DashboardSchemaVersion;
  sourceModelSchemaVersion: ModelSchemaVersion;
  analysisId: string;
  requestId: string;
  incidentId: string;
  parser: {
    backend: string;
    incidentTypes: string[];
    substanceMentions: Array<{
      surfaceText: string;
      role: 'INCIDENT' | 'FACILITY' | 'UNKNOWN';
      assertion: 'AFFIRMED' | 'NEGATED' | 'SUSPECTED' | 'UNKNOWN';
    }>;
    warning: string;
  };
  substanceCandidates: Array<{
    surfaceText: string;
    role: 'INCIDENT' | 'FACILITY' | 'UNKNOWN';
    resolverStatus: string;
    candidates: Array<{
      casNumber: string;
      rankingScore?: number | null;
      rankingScoreIsProbability: false;
      ruleEligible: false;
      currentInventoryConfirmed: false;
    }>;
    requiresResponderConfirmation: true;
  }>;
  facilityHistory: {
    status: 'CANDIDATES_FOUND' | 'NO_HISTORY_MATCH' | 'NOT_QUERIED';
    label: '과거 공개 이력 기반 시설물질 후보';
    semantics: 'HISTORICAL_CANDIDATE_NOT_CURRENT_INVENTORY';
    warning: string;
    candidates: Array<{
      facilityName: string;
      address?: string | null;
      province?: string | null;
      casNumber: string;
      chemicalNames?: string | null;
      latestSurveyYear?: string | null;
      sourceUrl?: string | null;
      evidenceClass: 'REPORTED_HANDLING_HISTORY';
      currentInventoryConfirmed: false;
      ruleEligible: false;
      requiresOnSiteConfirmation: true;
    }>;
  };
  evidenceCards: EvidenceCard[];
  /** 대응 근거 카드: statements와 citations만 화면에 표시하면 된다. */
  groundedRag?: GroundedRagSummary | null;
  /** 전환 기간에는 optional이며 신규 BE는 모델 API 값을 그대로 투영한다. */
  agent?: OperationsAgentSnapshot | null;
  confirmationGate: {
    policy: 'TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED';
    incidentConfirmed: boolean;
    facilityConfirmed: boolean;
    allRequiredConfirmed: boolean;
    ruleExecutionAllowed: boolean;
  };
  requiredNextSteps: string[];
  provenance: {
    modelVersion: string;
    dataVersion: string;
    rulePolicy: string;
    expertReviewed: false;
    finalDecisionAuthority: '현장 지휘관';
  };
  safetyNotice: string;
}

export interface AwaitingAnalysisResponse extends AnalysisCommon {
  state:
    | 'AWAITING_SUBSTANCE_CONFIRMATION'
    | 'AWAITING_INCIDENT_CONFIRMATION'
    | 'AWAITING_FACILITY_CONFIRMATION';
  conflictReview: {
    executed: false;
    status: 'NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS';
    missingConfirmations: Array<'incident_cas' | 'facility_cas'>;
    reason: string;
    riskDisplayAllowed: false;
  };
  riskDisplayAllowed: false;
}

export interface ReferenceAssurance {
  schemaVersion: 'chemicheck119-reference-assurance-v1';
  policyId: 'OFFICIAL_REFERENCE_TRIANGULATION_V1';
  status: 'REFERENCE_TRIANGULATED' | 'PRIMARY_AUTHORITY_ONLY';
  claimId?: string | null;
  claimType?: string | null;
  casPair: [string, string];
  claimTextKo?: string | null;
  expectedGasProducts?: string[] | null;
  scopeConditions?: string[] | null;
  notProvenByClaim?: string[] | null;
  referenceCount: number;
  independentAuthorityCount: number;
  sources: Array<{
    sourceId: string;
    authorityId: string;
    organization: string;
    independenceGroup: string;
    authorityKind: string;
    sourceRole:
      | 'PRIMARY_REACTIVITY_DATASHEET'
      | 'INCIDENT_OR_PUBLIC_HEALTH_CORROBORATION'
      | 'INTERNATIONAL_CHEMICAL_SAFETY_CARD';
    title: string;
    sourceUrl: string;
    locator: string;
    publishedOrUpdated: string;
    relation:
      | 'SUPPORTS'
      | 'SUPPORTS_WITH_INCIDENT_AND_MECHANISM'
      | 'SUPPORTS_SCREENING_ONLY';
  }>;
  claimChecks: Array<{
    claim:
      | 'SUBSTANCE_IDENTITY_AND_FORM'
      | 'PAIR_REACTIVITY_SCREENING'
      | 'CURRENT_SITE_INVENTORY'
      | 'ACTUAL_MIXING_AND_FIELD_CONDITIONS'
      | 'HUMAN_CHEMICAL_EXPERT_REVIEW';
    status: 'PASSED' | 'LIMITED' | 'NOT_PROVEN' | 'NOT_PERFORMED';
    basis: string;
  }>;
  registrySha256: string;
  reviewedAtUtc: string;
  machineChecked: true;
  expertReviewed: false;
  humanExpertSubstitute: false;
  decisionSupportOnly: true;
  limitations: string[];
}

export interface OrdinalRiskResult {
  /**
   * 아래 화학 상세 필드는 config/dashboard_public_pair_contract.json과
   * 물질쌍별로 정확히 일치해야 하며 FE/BE가 재생성하지 않는다.
   */
  kind: 'ORDINAL_SCREENING_RESULT';
  status: 'SCREENING_COMPLETED';
  scope: 'PUBLIC_SOURCE_CAMEO_SCREENING';
  policyMode: 'PUBLIC_SOURCE_PILOT_V1';
  incidentCas: string;
  facilityCas: string;
  ruleId: 'CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX';
  ruleVersion: 'RUNTIME_MANIFEST_PINNED';
  severity: 'NO_KNOWN_HAZARDOUS_REACTION' | 'CAUTION' | 'HIGH_RISK';
  riskLevel: 'LOW' | 'MEDIUM' | 'HIGH';
  riskLevelKo: '낮음' | '중간' | '높음';
  riskScale: {
    type: 'ORDINAL_CAMEO_COMPATIBILITY_CLASS';
    rawClassId?: number | null;
    isProbability: false;
    probabilityPercent: null;
    lowMeansSafe: false;
  };
  hazardCodes: Array<'C' | 'E' | 'F' | 'G' | 'NR' | 'R1' | 'R2' | 'R3' | 'T'>;
  gasProducts?: Array<
    | 'Acid Fumes'
    | 'BR2'
    | 'CO'
    | 'CO2'
    | 'Cl2'
    | 'ClO2'
    | 'H2'
    | 'HX'
    | 'Halocarbons'
    | 'Hydrocarbons'
    | 'NOx'
    | 'O2'
    | 'X2'
    | 'X2O'
    | 'XO2'
  > | null;
  briefText: string;
  requiredChecks: string[];
  evidenceUrls: string[];
  limitations: string[];
  finalDecision: '현장 지휘관 판단';
  expertReviewed: false;
  humanConfirmationRequired: true;
  mappingProvenance: Array<{
    role: 'INCIDENT' | 'FACILITY';
    casNumber: string;
    cameoChemicalId: string;
    selectedForm: string;
    verificationStatus: 'PUBLIC_SOURCE_VERIFIED';
    verificationMethod: 'EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET';
    evidenceUrl: string;
    sourceProduct: 'NOAA/EPA CAMEO Chemicals';
    sourceVersion: '3.1.0 rev 1';
    checkedAtUtc: string;
  }>;
  evidenceProvenance: {
    basis: 'PUBLIC_OFFICIAL_SOURCE';
    sourceProduct: 'NOAA/EPA CAMEO Chemicals';
    sourceVersions: string[];
    mappingEvidenceUrls: string[];
    compatibilityEvidenceUrls: string[];
  };
  referenceAssurance: ReferenceAssurance;
}

export interface CompletedAnalysisResponse extends AnalysisCommon {
  state: 'SCREENING_COMPLETED';
  conflictReview: {
    executed: true;
    status: 'SCREENING_COMPLETED';
    result: OrdinalRiskResult;
    riskDisplayAllowed: true;
  };
  riskDisplayAllowed: true;
}

export interface InconclusiveAnalysisResponse extends AnalysisCommon {
  state: 'VERIFY_REQUIRED' | 'UNCLASSIFIED' | 'CAMEO_GROUP_SCREENING_ONLY';
  conflictReview: {
    executed: true;
    status: 'VERIFY_REQUIRED' | 'UNCLASSIFIED' | 'CAMEO_GROUP_SCREENING_ONLY';
    result: {
      kind: 'INCONCLUSIVE_RESULT';
      status: 'VERIFY_REQUIRED' | 'UNCLASSIFIED' | 'CAMEO_GROUP_SCREENING_ONLY';
      reason: string;
      humanConfirmationRequired: true;
    };
    riskDisplayAllowed: false;
  };
  riskDisplayAllowed: false;
}

export type IncidentAnalysisResponse =
  | AwaitingAnalysisResponse
  | CompletedAnalysisResponse
  | InconclusiveAnalysisResponse;

export interface ConfirmationRequest {
  role: 'INCIDENT' | 'FACILITY';
  casNumber: string;
  displayName?: string | null;
  confirmationBasis:
    | 'CONTAINER_LABEL'
    | 'SITE_MSDS'
    | 'SHIPPING_DOCUMENT'
    | 'INSTRUMENT_READING'
    | 'RESPONDER_OBSERVATION'
    | 'OTHER_VERIFIED_SOURCE';
  observedAt: string;
}

export interface ConfirmationResponse {
  schemaVersion: DashboardSchemaVersion;
  requestId: string;
  incidentId: string;
  confirmationId: string;
  role: 'INCIDENT' | 'FACILITY';
  casNumber: string;
  createdAt: string;
  reanalyzeRequired: true;
}

export interface RecordSaveRequest {
  conversationStartedAt: string;
  messages: Array<{
    messageId: string;
    sequence: number;
    role: 'USER' | 'ASSISTANT' | 'SYSTEM';
    text: string;
    createdAt: string;
    analysisId?: string | null;
  }>;
  analysisIds: string[];
  confirmationIds: string[];
}

export interface RecordSaveResponse {
  schemaVersion: DashboardSchemaVersion;
  requestId: string;
  incidentId: string;
  recordId: string;
  savedAt: string;
  resetAllowed: true;
}
