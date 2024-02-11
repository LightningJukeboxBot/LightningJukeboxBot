import { darkRed, superDark } from "./setup";
import {
  createDiv,
  createPSetText,
  createSvg,
  makeIconPath,
  makeLinearGradient,
  setDisplayFlex,
  setMarginBottom,
} from "./utils";

export {};

const MARGINBOTTOMSM = "0.2rem";
const body = document.body;

const createPopUp = () => {
  const container = createDiv();
  const setContainerStyle = () => {
    const style = container.style;
    style.backgroundImage = makeLinearGradient(darkRed, superDark);
    style.height = "100%";
    style.fontFamily = "boucherie-block";
    style.color = "white";
    style.display = "flex";
    style.flexDirection = "column";
    style.alignContent = "center";
    style.textAlign = "center";
    style.padding = "0.8rem";
  };
  setContainerStyle();

  const createTitle = () => {
    return createPSetText("INFO");
  };
  const createSearchIcon = () => {
    const searchicon = createSvg(makeIconPath("red-search"), "24px");
    setMarginBottom(MARGINBOTTOMSM)(searchicon);
    return searchicon;
  };
  const createFirstInfo = () =>
    createPSetText("USE THE SEARCH FUNCTION AND SELECT THE BEST RESULT");
  const createUpDownIconst = () => {
    const container = createDiv();
    const setContainerStyle = () => {
      setDisplayFlex(container);
      const style = container.style;
      style.placeContent = "center";
    };
    setContainerStyle();
    const downpath = makeIconPath("downvote-arrow");
    const uppath = makeIconPath("upvote-arrow");
    [downpath, uppath]
      .map((e, i) => {
        const container = createDiv();
        const svg = createSvg(e);
        const setSvgStyle = () => {
          const style = svg.style;
          if (i == 0) style.marginRight = MARGINBOTTOMSM;
          style.padding = "0.2rem 0.4rem";
          style.backgroundColor = "white";
          style.borderRadius = "5px";
        };
        setSvgStyle();
        container.appendChild(svg);
        return container;
      })
      .forEach((e) => container.appendChild(e));
    setMarginBottom(MARGINBOTTOMSM)(container);
    return container;
  };
  const createSecondInfo = () =>
    createPSetText(`YOU MAY CHOOSE TO UP- OR DOWNVOTE TRACKS IN THE QUEUE`);
  const createInfoIcon = () => {
    const svg = createSvg(makeIconPath("info"), "24px");
    setMarginBottom(MARGINBOTTOMSM)(svg);
    return svg;
  };
  const createThirdInfo = () => {
    const container = createDiv();
    const firstLine = createPSetText(
      `THERE ARE THREE CHANNELS`,
      MARGINBOTTOMSM
    );
    const secondLine = createPSetText(`RED, GREEN AND BLUE`, MARGINBOTTOMSM);
    const thirdLine = createPSetText(`EACH CHANNEL HAS ITS OWN QUEUE PRICE`);
    [firstLine, secondLine, thirdLine].forEach((e) => container.appendChild(e));
    return container;
  };
  const createHaveFun = () => createPSetText("HAVE FUN!");
  container.appendChild(createTitle());
  container.appendChild(createSearchIcon());
  container.appendChild(createFirstInfo());
  container.appendChild(createUpDownIconst());
  container.appendChild(createSecondInfo());
  container.appendChild(createInfoIcon());
  container.appendChild(createThirdInfo());
  container.appendChild(createHaveFun());

  return container;
};

const popup = createPopUp();

body.prepend(popup);
