import { api } from "./api";
import { mergeActivity } from "./turnState";
import type { ActivityItem, TaskExecution } from "./types";

const PAGE_SIZE = 500;

type ActivityPage = {
  events: ActivityItem[];
  last_seq: number;
};

type ActivityPageLoader = (
  workspace: string,
  taskId: string,
  afterSeq: number,
  limit: number,
) => Promise<ActivityPage>;

export function mergeTaskActivities(
  durable: ActivityItem[],
  live: ActivityItem[],
): ActivityItem[] {
  return [...durable, ...live]
    .reduce<ActivityItem[]>((items, item) => mergeActivity(items, item), [])
    .sort((left, right) => left.seq - right.seq);
}

export function activitiesForExecution(
  items: ActivityItem[],
  execution: TaskExecution,
): ActivityItem[] {
  return items.filter((item) => item.subtask_id === execution.id);
}

export async function loadTaskActivityHistory(
  workspace: string,
  taskId: string,
  loadPage: ActivityPageLoader = api.taskEvents,
): Promise<ActivityItem[]> {
  let afterSeq = 0;
  let activities: ActivityItem[] = [];

  for (;;) {
    const page = await loadPage(workspace, taskId, afterSeq, PAGE_SIZE);
    const events = Array.isArray(page.events) ? page.events : [];
    activities = mergeTaskActivities(activities, events);
    const lastSeq = Number(page.last_seq || events.at(-1)?.seq || afterSeq);
    if (events.length < PAGE_SIZE || lastSeq <= afterSeq) break;
    afterSeq = lastSeq;
  }

  return activities;
}
