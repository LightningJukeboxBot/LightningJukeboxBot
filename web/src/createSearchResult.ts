import { SearchResultSettings } from "./setup";
import {
  createDiv,
  createP,
  setBorderRadius,
  setElementBgColor,
  setElementStyle,
  setMargin1rem,
} from "./utils";

export const createSearchResult = (
  settings: SearchResultSettings,
  results?: string[]
): [HTMLElement, eventCb[], colorCb, resultSetter] => {
  const container = createDiv();
  container.id = settings.container.id;
  container.style.overflow = settings.container.style.overflow;
  container.style.backgroundColor = settings.container.style.bgColor;
  setBorderRadius(settings.container.style.borderRadius)(container);
  setMargin1rem(container);
  const createResult = (result: string, i: number): [HTMLElement, eventCb] => {
    const container = createDiv();
    container.setAttribute(settings.container.attribute.key, i.toString());
    container.style.backgroundColor =
      settings.container.result.style.backgroundColor;
    const text = createP();
    setElementStyle(container, text);
    text.textContent = result;
    container.appendChild(text);
    const setSearchResultCb = (cb: (e: MouseEvent) => void) => {
      container.onclick = cb;
    };
    return [container, setSearchResultCb];
  };
  const setResultBackgroundColor = setElementBgColor(container);
  const showResult = (res: string[]) => {
    const resultsElements = res.map(createResult);
    resultsElements.forEach((e) => container.appendChild(e[0]));
    return resultsElements;
  };
  if (results) {
    const resultsElements = showResult(results);
    return [
      container,
      resultsElements.map((e) => e[1]),
      setResultBackgroundColor,
      showResult,
    ];
  } else {
    return [container, [], setResultBackgroundColor, showResult];
  }
};
