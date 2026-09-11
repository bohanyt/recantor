import { expect, test } from '@playwright/test';

declare global {
  interface Window {
    __emitTranscriptNotice?: (sequence: number) => void;
    __setTranscriptDeliveryAvailable?: (available: boolean) => void;
  }
}

test('live transcript converges through canonical HTTP state and archive survives delivery outage', async ({
  page,
}) => {
  type Segment = {
    id: string;
    sequence: number;
    start_ms: number;
    end_ms: number;
    text: string;
    language: string | null;
    created_at: string;
  };

  let canonical: Segment[] = [];
  await page.route('**/api/v1/sessions/*/transcript**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/transcript/live')) {
      await route.continue();
      return;
    }
    const afterSequence = Number(url.searchParams.get('after_sequence') ?? '0');
    const segments = canonical.filter((item) => item.sequence > afterSequence);
    const nextAfterSequence = segments.reduce(
      (highest, item) => Math.max(highest, item.sequence),
      afterSequence,
    );
    const sessionId = url.pathname.split('/').at(-2) ?? '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: sessionId,
        after_sequence: afterSequence,
        next_after_sequence: nextAfterSequence,
        has_more: false,
        segments,
      }),
    });
  });

  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    let deliveryAvailable = true;
    const transcriptSockets: Array<{
      readyState: number;
      onopen: ((event: Event) => void) | null;
      onmessage: ((event: MessageEvent) => void) | null;
      onerror: ((event: Event) => void) | null;
      onclose: ((event: CloseEvent) => void) | null;
      close: () => void;
    }> = [];

    class FakeTranscriptSocket {
      readyState = NativeWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;

      constructor() {
        transcriptSockets.push(this);
        queueMicrotask(() => {
          if (!deliveryAvailable) {
            this.readyState = NativeWebSocket.CLOSED;
            this.onerror?.(new Event('error'));
            this.onclose?.(new CloseEvent('close', { code: 1013 }));
            return;
          }
          this.readyState = NativeWebSocket.OPEN;
          this.onopen?.(new Event('open'));
          this.onmessage?.(
            new MessageEvent('message', { data: JSON.stringify({ type: 'ready' }) }),
          );
        });
      }

      send() {}

      close() {
        if (this.readyState === NativeWebSocket.CLOSED) return;
        this.readyState = NativeWebSocket.CLOSED;
        this.onclose?.(new CloseEvent('close', { code: 1000 }));
      }
    }

    function RoutedWebSocket(url: string | URL, protocols?: string | string[]) {
      if (!String(url).includes('/transcript/live')) {
        return protocols === undefined
          ? new NativeWebSocket(url)
          : new NativeWebSocket(url, protocols);
      }
      return new FakeTranscriptSocket();
    }
    Object.assign(RoutedWebSocket, {
      CONNECTING: NativeWebSocket.CONNECTING,
      OPEN: NativeWebSocket.OPEN,
      CLOSING: NativeWebSocket.CLOSING,
      CLOSED: NativeWebSocket.CLOSED,
      prototype: NativeWebSocket.prototype,
    });
    Object.defineProperty(window, 'WebSocket', {
      configurable: true,
      value: RoutedWebSocket,
    });

    window.__emitTranscriptNotice = (sequence: number) => {
      for (const socket of transcriptSockets) {
        if (socket.readyState !== NativeWebSocket.OPEN) continue;
        socket.onmessage?.(
          new MessageEvent('message', {
            data: JSON.stringify({ type: 'transcript_available', sequence }),
          }),
        );
      }
    };
    window.__setTranscriptDeliveryAvailable = (available: boolean) => {
      deliveryAvailable = available;
      if (available) return;
      for (const socket of transcriptSockets) {
        if (socket.readyState !== NativeWebSocket.OPEN) continue;
        socket.onmessage?.(
          new MessageEvent('message', {
            data: JSON.stringify({ type: 'delivery_degraded' }),
          }),
        );
        socket.readyState = NativeWebSocket.CLOSED;
        socket.onclose?.(new CloseEvent('close', { code: 1013 }));
      }
    };
  });

  await page.goto('/');
  await page.getByTestId('start-recording').click();
  await expect(page.getByTestId('transcript-empty')).toContainText('Listening for speech');
  await expect(page.getByTestId('transcript-delivery-state')).toHaveText('Live');

  canonical = [
    {
      id: '00000000-0000-4000-8000-000000000003',
      sequence: 3,
      start_ms: 2_000,
      end_ms: 3_000,
      text: 'ketiga',
      language: 'id',
      created_at: '2026-09-11T00:00:03Z',
    },
    {
      id: '00000000-0000-4000-8000-000000000001',
      sequence: 1,
      start_ms: 0,
      end_ms: 1_000,
      text: 'pertama',
      language: 'id',
      created_at: '2026-09-11T00:00:01Z',
    },
    {
      id: '00000000-0000-4000-8000-000000000002',
      sequence: 2,
      start_ms: 1_000,
      end_ms: 2_000,
      text: 'kedua',
      language: 'id',
      created_at: '2026-09-11T00:00:02Z',
    },
  ];
  await page.evaluate(() => {
    window.__emitTranscriptNotice?.(3);
    window.__emitTranscriptNotice?.(2);
    window.__emitTranscriptNotice?.(3);
  });

  await expect(page.getByTestId('transcript-segment-1')).toContainText('pertama');
  await expect(page.getByTestId('transcript-segment-2')).toContainText('kedua');
  await expect(page.getByTestId('transcript-segment-3')).toContainText('ketiga');
  const orderedText = await page
    .getByTestId('transcript-segments')
    .locator('li')
    .allTextContents();
  expect(orderedText.join('|')).toMatch(/pertama.*kedua.*ketiga/);

  await page.evaluate(() => window.__setTranscriptDeliveryAvailable?.(false));
  await expect(page.getByTestId('transcript-delivery-state')).toHaveText(/Catching up|Delayed/);

  await page.waitForTimeout(2_600);
  await page.getByTestId('stop-recording').click();
  await expect(page.getByTestId('recorder-message')).toContainText('finalized', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('durability-state')).toContainText('Server synced');
  await expect(page.getByTestId('pending-chunks')).toContainText('0 fragments');
});
