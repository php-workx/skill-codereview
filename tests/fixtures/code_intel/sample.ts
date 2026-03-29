import { Request, Response } from "express";
import * as fs from "fs";

export function handleRequest(req: Request, res: Response): void {
  const data = req.body;
  res.json({ ok: true, data });
}

export class UserController {
  private cache: Map<string, any> = new Map();

  public getUser(id: string): any {
    return this.cache.get(id);
  }

  private refreshCache(): void {
    this.cache.clear();
  }
}

function internalHelper(value: string): string {
  return value.trim();
}

export const API_VERSION = "1.0.0";

export default function defaultExport(): string {
  return "default";
}
