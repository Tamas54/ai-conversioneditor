export interface Task { id: string; name: string; operation: string; status: "waiting" | "processing" | "finished" | "error" | "cancelled";
  depends_on: string[]; message?: string | null; code?: string | null; percent: number; credits: number; result?: any }
export interface Job { id: string; tag?: string | null; status: string; sandbox: boolean; credits: number; tasks: Task[] }
export interface FileOut { file_id: string; filename: string; size: number; format: string; url?: string }
export type TaskSpec = { operation: string; input?: string | string[]; output_format?: string; options?: Record<string, unknown>; [k: string]: unknown };
export class ApiError extends Error { status: number; code: string }
export class Client {
  constructor(baseUrl: string, opts?: { apiKey?: string; fetch?: typeof fetch });
  jobs: { create(tasks: Record<string, TaskSpec>, o?: { tag?: string; webhookUrl?: string }): Promise<Job>; get(id: string, wait?: number): Promise<Job>;
          list(q?: Record<string, string>): Promise<Job[]>; cancel(id: string): Promise<any>; wait(id: string, timeoutMs?: number): Promise<Job>;
          files(job: Job, task?: string): FileOut[] };
  webhooks: { list(): Promise<any>; create(url: string, events?: string[]): Promise<any>; delete(id: number): Promise<any> };
  me(): Promise<any>; targets(fmt: string): Promise<string[]>;
  convert(o: { url?: string; path?: string; outputFormat: string; options?: Record<string, unknown>; wait?: boolean }): Promise<Job>;
  upload(job: Job, taskName: string, path: string): Promise<any>; download(url: string, dest: string): Promise<string>;
  static verifyWebhook(secret: string, body: string | Buffer, signature: string, timestamp: string | number, toleranceS?: number): boolean;
}
