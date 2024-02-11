import { InfosSettings } from "./setup";
import {
  createDiv,
  createP,
  createSvg,
  makeIconPath,
  setDisplayFlex,
  setElementColor,
  setHeight6cqh,
} from "./utils";
import { tabColorsMap } from "./lib/tabColorsMap";

export const createInfos = (
  text: string,
  settings: InfosSettings
): [HTMLElement, eventCb, colorCb, textCb] => {
  const container = createDiv();
  container.id = settings.container.id;
  container.style.placeItems = settings.container.placeItems;
  container.style.placeContent = settings.container.placeContent;
  setHeight6cqh(container);
  setDisplayFlex(container);
  const infosvg = createSvg(makeIconPath(settings.iconFilename));
  const infos = createP();
  infosvg.style.cursor = settings.icon.cursor;
  const setOnClick = (cb: (e: MouseEvent) => void) => (infosvg.onclick = cb);
  const setText = (text: string) => (infos.textContent = text);
  infos.textContent = setText(text);
  infos.style.fontSize = settings.p.fontSize;
  infos.style.marginLeft = settings.p.marginLeft;
  infos.style.color = settings.p.color;
  const setInfosTextColor = setElementColor(infos);
  container.appendChild(infosvg);
  container.appendChild(infos);
  return [container, setOnClick, setInfosTextColor, setText];
};
