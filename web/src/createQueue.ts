import { innerRed, gridTemplate, vote, QueueSettings } from "./setup";
import {
  createButton,
  createDiv,
  createP,
  makeIconPath,
  setBorderRadius,
  setDisplayGrid,
  setElementBgColor,
  setElementColor,
  setElementStyle,
  setMargin1rem,
} from "./utils";

export const createQueue = (
  currentqueue: string[],
  settings: QueueSettings
): [HTMLElement, [eventCb[], eventCb[]], colorCb, colorCb, colorCb] => {
  const container = createDiv();
  container.style.overflow = settings.container.style.overflow;
  setDisplayGrid(container);
  container.id = settings.container.id;
  const queueheader = createDiv();
  queueheader.id = settings.queueheader.id;
  setDisplayGrid(queueheader);
  queueheader.style.placeItems = settings.queueheader.style.placeItems;
  const createVote = () => {
    const vote = createP();
    vote.textContent = settings.vote.textContent;
    vote.style.color = innerRed;
    vote.style.fontSize = settings.vote.style.fontSize;
    return vote;
  };
  const voteL = createVote();
  const voteR = createVote();
  const setVoteColor = (color: string) => {
    [voteL, voteR].forEach((e) => setElementColor(e)(color));
  };
  const queue = createP();
  queue.textContent = settings.queue.textContent;
  if (vote) {
    queueheader.style.gridTemplateColumns = gridTemplate;
    queueheader.style.margin = settings.queueheader.style.margin;
    queueheader.appendChild(voteL);
    queueheader.appendChild(queue);
    queueheader.appendChild(voteR);
  } else {
    queueheader.appendChild(queue);
  }
  const content = createDiv();
  setMargin1rem(content);
  content.id = settings.content.id;
  content.style.overflow = settings.content.style.overflow;
  content.style.backgroundColor = settings.content.style.backgroundColor;
  const setQueueBackgroundColor = setElementBgColor(content);
  setBorderRadius(settings.content.style.borderRadius)(content);
  const createQueueElement = (
    infostext: string,
    i: number
  ): [HTMLElement] | [HTMLElement, eventCb, eventCb] => {
    const element = createDiv();
    const infos = createP();
    infos.style.backgroundColor = settings.infos.style.backgroundColor;
    infos.style.width = settings.infos.style.width;
    setBorderRadius(settings.infos.style.borderRadius)(infos);
    const margins = settings.infos.style.magins;
    infos.style.marginLeft = margins;
    infos.style.marginRight = margins;
    element.id = settings.infos.element.id;
    element.setAttribute(settings.infos.element.attribute.key, i.toString());
    infos.textContent = infostext;
    setElementStyle(element, infos);
    if (vote) {
      setDisplayGrid(element);
      element.style.gridTemplateColumns = gridTemplate;
      const upButton = createButton(
        makeIconPath(settings.infos.upvoteFilename)
      );
      const setUpButtonCb = (cb: (e: MouseEvent) => void) => {
        upButton.onclick = cb;
      };
      const downButton = createButton(
        makeIconPath(settings.infos.downvoteFilename)
      );
      const setDownButtonCb = (cb: (e: MouseEvent) => void) => {
        downButton.onclick = cb;
      };
      element.appendChild(downButton);
      element.appendChild(infos);
      element.appendChild(upButton);
      infos.style.placeSelf = settings.infos.style.placeSelf;
      return [element, setDownButtonCb, setUpButtonCb];
    } else {
      element.appendChild(infos);
      return [element];
    }
  };
  const tracksnumber = createP();
  const tracks = 25;
  tracksnumber.textContent = settings.tracksNumber.textContent(
    tracks.toString()
  );
  tracksnumber.style.placeSelf = settings.tracksNumber.style.placeSelf;
  tracksnumber.style.color = innerRed;
  tracksnumber.style.fontSize = settings.tracksNumber.style.fontSize;
  const setTrackInfoColor = (color: string) =>
    setElementColor(tracksnumber)(color);
  content.style.marginTop = settings.content.style.marginTop;
  content.style.marginBottom = settings.content.style.marginBottom;
  const queuelements = currentqueue.map(createQueueElement);
  queuelements.forEach((e) => content.appendChild(e[0]));
  container.appendChild(queueheader);
  container.appendChild(content);
  container.appendChild(tracksnumber);
  return [
    container,
    [queuelements.map((e) => e[1]!), queuelements.map((e) => e[2]!)],
    setQueueBackgroundColor,
    setVoteColor,
    setTrackInfoColor,
  ];
};
