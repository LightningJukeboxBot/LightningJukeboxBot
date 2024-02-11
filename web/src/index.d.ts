type eventCb = (cb: (e: MouseEvent) => void) => void;
type colorCb = (color: string) => void;
type trackCb = (track: string) => void;
type textCb = (text: string) => void;
type resultSetter = (res: string[]) => [HTMLElement, eventCb][];
