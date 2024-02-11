import { gradientMap } from "./lib/gradientMap";
import { DisplaySettings } from "./setup";
import { createDiv } from "./utils";

export const createDisplay = (
  settings: DisplaySettings
): [HTMLElement, colorCb] => {
  const container = createDiv();
  container.id = settings.container.id;
  container.style.height = settings.container.height;
  container.style.containerType = settings.container.containerType;
  const setColor = (color: string) =>
    (container.style.backgroundImage = gradientMap.get(color));
  return [container, setColor];
};
