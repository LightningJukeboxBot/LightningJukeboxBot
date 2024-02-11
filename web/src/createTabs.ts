import { greencolor, TabsSettings } from "./setup";
import {
  createDiv,
  createP,
  createSvg,
  makeIconPath,
  setDisplayFlex,
} from "./utils";

export const createTabs = (
  settings: TabsSettings
): [HTMLElement, [eventCb, eventCb, eventCb], colorCb] => {
  const container = createDiv();
  container.id = settings.container.id;
  setDisplayFlex(container);
  const setContainerStyle = () => {
    const style = container.style;
    const { position, height } = settings.container;
    style.position = position;
    style.height = height;
  };
  setContainerStyle();
  // RED
  const red = createDiv();
  red.id = settings.redTab.id;
  const redsvg = createSvg(
    makeIconPath(settings.redTab.iconFilename),
    settings.tabSize
  );
  redsvg.id = settings.redTab.icon.id;
  red.appendChild(redsvg);
  red.style.backgroundColor = settings.redTab.bgColor;
  // GREEN
  const green = createDiv();
  green.id = settings.greenTab.id;
  const greenp = createP();
  const greensvg = createSvg(
    makeIconPath(settings.greenTab.iconFilename),
    settings.tabSize
  );
  green.appendChild(greensvg);
  greensvg.id = settings.greenTab.icon.id;
  const gr = greencolor;
  greenp.textContent = gr;
  green.style.backgroundColor = settings.greenTab.bgColor;
  green.style.left = settings.greenTab.left!;
  // BLUE
  const blue = createDiv();
  blue.id = settings.blueTab.id;
  const bluesvg = createSvg(
    makeIconPath(settings.blueTab.iconFilename),
    settings.tabSize
  );
  bluesvg.id = settings.blueTab.icon.id;
  blue.appendChild(bluesvg);
  blue.style.backgroundColor = settings.blueTab.bgColor;
  blue.style.left = settings.blueTab.left!;
  const setTabStyle = (tab: HTMLElement, active: boolean = true) => {
    const style = tab.style;
    style.width = settings.width;
    style.position = settings.position;
    style.bottom = settings.bottom;
    style.height = settings.height(active);
    style.borderRadius = settings.borderRadius;
    style.display = settings.display;
    style.placeItems = settings.placeItems;
    style.transition = settings.transition;
  };
  const setRedCb = (cb: (e: MouseEvent) => void) => {
    red.onclick = cb;
  };
  const setGreebCb = (cb: (e: MouseEvent) => void) => {
    green.onclick = cb;
  };
  const setBlueCb = (cb: (e: MouseEvent) => void) => {
    blue.onclick = cb;
  };
  const setTabsHeights = (color: string) => {
    [red, green, blue].forEach((e) => {
      if (e.id === color) {
        console.log(e.id);
        setTabStyle(e);
      } else {
        setTabStyle(e, false);
      }
    });
  };
  [red, green, blue].forEach((e) => setTabStyle(e));
  container.appendChild(red);
  container.appendChild(green);
  container.appendChild(blue);
  return [container, [setRedCb, setGreebCb, setBlueCb], setTabsHeights];
};
