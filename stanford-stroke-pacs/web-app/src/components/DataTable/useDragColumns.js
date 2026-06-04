import { useState, useRef } from "react";

export default function useDragColumns(reorder) {
  const dragColKey = useRef(null);
  const [dragOverKey, setDragOverKey] = useState(null);
  const [dropSide, setDropSide] = useState(null);

  const handleDragStart = (key, e) => {
    dragColKey.current = key;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", key);
  };

  const handleDragOver = (key, e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (dragColKey.current === null || dragColKey.current === key) {
      setDragOverKey(null);
      setDropSide(null);
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    const side = e.clientX < rect.left + rect.width / 2 ? "before" : "after";
    setDragOverKey(key);
    setDropSide(side);
  };

  const handleDragLeave = () => {
    setDragOverKey(null);
    setDropSide(null);
  };

  const handleDrop = (key, e) => {
    e.preventDefault();
    const fromKey = dragColKey.current;
    if (fromKey && fromKey !== key) {
      const rect = e.currentTarget.getBoundingClientRect();
      const side = e.clientX < rect.left + rect.width / 2 ? "before" : "after";
      reorder(fromKey, key, side);
    }
    dragColKey.current = null;
    setDragOverKey(null);
    setDropSide(null);
  };

  const handleDragEnd = () => {
    dragColKey.current = null;
    setDragOverKey(null);
    setDropSide(null);
  };

  return {
    dragColKey,
    dragOverKey,
    dropSide,
    handleDragStart,
    handleDragOver,
    handleDragLeave,
    handleDrop,
    handleDragEnd,
  };
}
