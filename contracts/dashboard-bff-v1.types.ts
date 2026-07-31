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
  } | null;
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
