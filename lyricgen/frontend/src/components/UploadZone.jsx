import { useRef, useState } from "react";

export default function UploadZone({ file, onFile }) {
  const inputRef = useRef();
  const [dragging, setDragging] = useState(false);

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f && f.name.toLowerCase().endsWith(".mp3")) {
      onFile(f);
    }
  };

  const handleChange = (e) => {
    const f = e.target.files[0];
    if (f) onFile(f);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => inputRef.current.click()}
      className={`border-2 border-dashed rounded-2xl p-10 text-center cursor-pointer transition
        ${dragging ? "border-brand bg-brand/10" : "border-gray-600 hover:border-brand/50"}
        ${file ? "bg-surface-light" : ""}`}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".mp3"
        className="hidden"
        onChange={handleChange}
      />
      {file ? (
        <div>
          <p className="text-brand text-lg font-medium">{file.name}</p>
          <p className="text-gray-500 text-sm mt-1">
            {(file.size / 1024 / 1024).toFixed(1)} MB — Click para cambiar
          </p>
        </div>
      ) : (
        <div>
          <p className="text-gray-300 text-lg">
            Arrastra un archivo MP3 aquí
          </p>
          <p className="text-gray-500 text-sm mt-1">o haz click para seleccionar</p>
        </div>
      )}
    </div>
  );
}
