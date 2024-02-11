import { WebUiSettings } from "./setup";
import { createDiv } from "./utils";

export const createWebui = (settings: WebUiSettings) => {
  const container = createDiv();
  container.id = settings.container.id;
  const setWebUIStyle = () => {
    const { height, containerType, fontFamily, color } =
      settings.container.style;
    const style = container.style;
    style.height = height;
    style.containerType = containerType;
    style.fontFamily = fontFamily;
    style.color = color;
  };
  setWebUIStyle();
  return container;
};
