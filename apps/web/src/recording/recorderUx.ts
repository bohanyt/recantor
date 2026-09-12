import type { FencedRecorderSnapshot } from '../recorder/fencedController';

export type StatusTone = 'neutral' | 'positive' | 'warning' | 'danger';

export type ProductStatus = {
  label: string;
  detail: string;
  tone: StatusTone;
};

export type RecorderActionId =
  | 'start'
  | 'stop'
  | 'defer-finalization'
  | 'resume'
  | 'finish-recovered'
  | 'declare-missing-gaps'
  | 'sync';

export type RecorderActionPlan = {
  primary: RecorderActionId | null;
  secondary: RecorderActionId[];
  danger: RecorderActionId[];
};

export type RecorderUx = {
  lifecycle: ProductStatus;
  audio: ProductStatus;
  transcription: ProductStatus;
  actions: RecorderActionPlan;
};

function lifecycleStatus(snapshot: FencedRecorderSnapshot): ProductStatus {
  if (snapshot.captureFenced) {
    return {
      label: 'Needs attention',
      detail: 'Recording ownership changed. This tab will not start new capture or mutate retained evidence.',
      tone: 'warning',
    };
  }

  switch (snapshot.phase) {
    case 'requesting':
      return {
        label: 'Starting microphone…',
        detail: 'Preparing this recording session.',
        tone: 'neutral',
      };
    case 'recording':
      return {
        label: 'recording',
        detail: 'Microphone capture is active.',
        tone: 'positive',
      };
    case 'finalizing':
      return {
        label: 'Finalizing',
        detail: 'Finishing the declared recording boundary and durable audio sync.',
        tone: 'neutral',
      };
    case 'recoverable':
      return {
        label: 'Needs recovery',
        detail: 'A previous recording can be resumed or finished from retained evidence.',
        tone: 'warning',
      };
    case 'complete':
      return {
        label: 'Complete',
        detail: 'The recording reached a terminal server state.',
        tone: snapshot.gapCount > 0 ? 'warning' : 'positive',
      };
    case 'error':
      return {
        label: 'Ready to retry',
        detail: 'The last start attempt failed before active capture. Try again when ready.',
        tone: 'warning',
      };
    case 'idle':
    default:
      return {
        label: 'Ready',
        detail: 'Start when everyone is ready to record.',
        tone: 'neutral',
      };
  }
}

function audioStatus(snapshot: FencedRecorderSnapshot): ProductStatus {
  if (snapshot.captureFenced && snapshot.pendingChunks > 0) {
    return {
      label: 'Audio retained here — action blocked',
      detail:
        'This tab no longer owns the session. Local audio is retained as evidence and is not safe to sync under the stale capture generation.',
      tone: 'danger',
    };
  }
  if (snapshot.captureFenced) {
    return {
      label: 'Session ownership changed',
      detail: 'This tab no longer owns capture. Unsafe recovery actions are withheld.',
      tone: 'warning',
    };
  }
  if (snapshot.localWriteFailed) {
    return {
      label: 'Audio storage unsafe',
      detail: 'The browser recovery spool failed. Do not assume unsynced audio is safely retained.',
      tone: 'danger',
    };
  }
  if (snapshot.missingSequences.length > 0) {
    return {
      label: 'Audio missing — action required',
      detail: 'The server still expects one or more audio sequences. Retry recovery or explicitly declare confirmed loss.',
      tone: 'danger',
    };
  }
  if (snapshot.pendingChunks > 0) {
    if (!snapshot.online) {
      return {
        label: 'Audio saved locally',
        detail: snapshot.storage?.persisted
          ? 'The network is offline. Unsynced audio is retained in persistent browser storage and will retry when connectivity returns.'
          : 'The network is offline. Unsynced audio is retained locally on a best-effort basis until it can sync.',
        tone: snapshot.storage?.persisted ? 'warning' : 'danger',
      };
    }
    return {
      label: snapshot.phase === 'recoverable' ? 'Recovering audio' : 'Syncing audio',
      detail: `${snapshot.pendingChunks} emitted audio fragment${snapshot.pendingChunks === 1 ? ' is' : 's are'} waiting for durable server acknowledgement.`,
      tone: 'warning',
    };
  }
  if (snapshot.phase === 'complete' && snapshot.gapCount > 0) {
    return {
      label: 'Audio finalized with declared loss',
      detail: 'The server has terminal audio evidence, including an explicitly declared continuity gap.',
      tone: 'warning',
    };
  }
  if (snapshot.sessionId && snapshot.phase === 'recoverable') {
    return {
      label: 'Audio recovery ready',
      detail: 'Known local audio is synced; choose whether to resume capture or finish the recovered recording.',
      tone: 'warning',
    };
  }
  if (snapshot.sessionId) {
    return {
      label: 'Audio synced',
      detail: 'Server synced: all emitted archive audio known to this tab is durably acknowledged.',
      tone: 'positive',
    };
  }
  return {
    label: 'Audio ready',
    detail: 'No recording is active yet.',
    tone: 'neutral',
  };
}

function transcriptionStatus(snapshot: FencedRecorderSnapshot): ProductStatus {
  if (!snapshot.sessionId) {
    return {
      label: 'Not started',
      detail: 'Transcription starts downstream after a live recording session exists.',
      tone: 'neutral',
    };
  }
  if (snapshot.realtimeStatus === 'connecting') {
    return {
      label: 'Starting transcription',
      detail: 'The realtime speech lane is connecting. Archive-audio safety is tracked separately.',
      tone: 'neutral',
    };
  }
  if (snapshot.realtimeStatus === 'live') {
    return {
      label: 'Transcribing',
      detail: 'Realtime speech work is active. Canonical committed transcript text appears in the transcript panel.',
      tone: 'positive',
    };
  }
  if (snapshot.realtimeStatus === 'degraded' || snapshot.realtimeError) {
    return {
      label: 'Transcription delayed',
      detail: 'Live speech processing is degraded. Archive recording continues independently and canonical transcript recovery can catch up later.',
      tone: 'warning',
    };
  }
  return {
    label: snapshot.phase === 'complete' ? 'Processing may continue' : 'Not active',
    detail:
      snapshot.phase === 'complete'
        ? 'Recording is finished, but this client does not claim downstream transcription is complete. Canonical committed text remains authoritative.'
        : 'The realtime speech lane is not active. This does not change archive-audio safety.',
    tone: 'neutral',
  };
}

export function deriveRecorderActions(snapshot: FencedRecorderSnapshot): RecorderActionPlan {
  if (snapshot.captureFenced) return { primary: null, secondary: [], danger: [] };

  const secondary: RecorderActionId[] = [];
  const danger: RecorderActionId[] = [];
  let primary: RecorderActionId | null = null;

  if (snapshot.phase === 'recording') {
    primary = 'stop';
  } else if (snapshot.phase === 'finalizing') {
    primary = 'defer-finalization';
  } else if (snapshot.phase === 'recoverable') {
    if (snapshot.missingSequences.length > 0) {
      primary = 'finish-recovered';
      danger.push('declare-missing-gaps');
    } else {
      primary = 'resume';
      secondary.push('finish-recovered');
    }
  } else if (snapshot.phase === 'idle' || snapshot.phase === 'complete' || snapshot.phase === 'error') {
    primary = 'start';
  }

  if (
    snapshot.sessionId &&
    snapshot.pendingChunks > 0 &&
    snapshot.phase !== 'requesting' &&
    snapshot.phase !== 'finalizing'
  ) {
    secondary.push('sync');
  }

  return { primary, secondary, danger };
}

export function deriveRecorderUx(snapshot: FencedRecorderSnapshot): RecorderUx {
  return {
    lifecycle: lifecycleStatus(snapshot),
    audio: audioStatus(snapshot),
    transcription: transcriptionStatus(snapshot),
    actions: deriveRecorderActions(snapshot),
  };
}
