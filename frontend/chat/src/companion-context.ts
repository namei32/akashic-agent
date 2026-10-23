import { createContext } from "react";

/** 只来自消息操作按钮；模型输出不能触发或替换阅读引用。 */
export const CompanionActions = createContext<((messageId: string) => void) | null>(null);
