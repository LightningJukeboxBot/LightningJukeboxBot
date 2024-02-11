import { fontMid, HeaderSettings } from "./setup";
import { createDiv, createP, setDisplayFlex, setHeight9cqh } from "./utils";

export const createHeader = (
  songinfostext: string,
  settings: HeaderSettings
): [HTMLElement, trackCb] => {
  const container = createDiv();
  container.style.placeItems = settings.container.placeItems;
  container.style.placeContent = settings.container.placeContent;
  container.id = settings.container.id;
  setHeight9cqh(container);
  const nowPlaying = createP();
  const makeText = settings.makeText;
  nowPlaying.textContent = makeText(songinfostext);
  const setNowPlayingStyle = () => {
    const style = nowPlaying.style;
    const margintb = settings.nowPlaying.margintb;
    const marginlr = settings.nowPlaying.marginlr;
    style.paddingTop = margintb;
    style.paddingRight = marginlr;
    style.paddingBottom = margintb;
    style.paddingLeft = marginlr;
    style.borderTop = settings.nowPlaying.borders;
    style.borderBottom = settings.nowPlaying.borders;
    style.fontSize = fontMid;
  };
  const setNowPlayingTrack: trackCb = (track) => {
    nowPlaying.textContent = makeText(track);
  };
  setNowPlayingStyle();
  setDisplayFlex(container);
  container.appendChild(nowPlaying);
  return [container, setNowPlayingTrack];
};
