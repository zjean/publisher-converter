// pubdump — Microsoft Publisher (.pub) to JSON event stream.
//
// Implements librevenge's RVNGDrawingInterface and serialises every callback
// libmspub emits into newline-delimited JSON. This isolates all binary-format
// parsing in C++ (via libmspub) and leaves document reconstruction and IDML
// generation to Python, where it is far easier to iterate and test.
//
// Output: one JSON object per line, {"e": <callback>, "p": {<properties>}}.
// Property values are strings; nested property-list vectors become arrays of
// objects. Binary payloads (images) arrive base64-encoded from librevenge.

#include <cstdio>
#include <cstdlib>
#include <string>

#include <librevenge/librevenge.h>
#include <librevenge-stream/librevenge-stream.h>
#include <libmspub/libmspub.h>

namespace
{

void jsonEscape(const char *s, std::string &out)
{
  if (!s)
    return;
  for (const unsigned char *p = reinterpret_cast<const unsigned char *>(s); *p; ++p)
  {
    switch (*p)
    {
    case '"':
      out += "\\\"";
      break;
    case '\\':
      out += "\\\\";
      break;
    case '\n':
      out += "\\n";
      break;
    case '\r':
      out += "\\r";
      break;
    case '\t':
      out += "\\t";
      break;
    case '\b':
      out += "\\b";
      break;
    case '\f':
      out += "\\f";
      break;
    default:
      if (*p < 0x20)
      {
        // Control characters must be escaped; UTF-8 continuation bytes
        // (>= 0x80) pass through untouched.
        char buf[8];
        std::snprintf(buf, sizeof(buf), "\\u%04x", *p);
        out += buf;
      }
      else
        out += static_cast<char>(*p);
    }
  }
}

void jsonString(const char *s, std::string &out)
{
  out += '"';
  jsonEscape(s, out);
  out += '"';
}

void serialiseList(const librevenge::RVNGPropertyList &propList, std::string &out);

void serialiseVector(const librevenge::RVNGPropertyListVector &vec, std::string &out)
{
  out += '[';
  for (unsigned long i = 0; i < vec.count(); ++i)
  {
    if (i)
      out += ',';
    serialiseList(vec[i], out);
  }
  out += ']';
}

void serialiseList(const librevenge::RVNGPropertyList &propList, std::string &out)
{
  out += '{';
  bool first = true;
  librevenge::RVNGPropertyList::Iter it(propList);
  for (it.rewind(); it.next();)
  {
    if (!first)
      out += ',';
    first = false;
    jsonString(it.key(), out);
    out += ':';
    if (const librevenge::RVNGPropertyListVector *child = it.child())
      serialiseVector(*child, out);
    else
      jsonString(it()->getStr().cstr(), out);
  }
  out += '}';
}

class JsonPainter : public librevenge::RVNGDrawingInterface
{
public:
  JsonPainter() = default;

private:
  // Emits a bare event with no payload.
  void emit(const char *event)
  {
    std::string line = "{\"e\":";
    jsonString(event, line);
    line += "}\n";
    std::fwrite(line.data(), 1, line.size(), stdout);
  }

  void emit(const char *event, const librevenge::RVNGPropertyList &propList)
  {
    std::string line = "{\"e\":";
    jsonString(event, line);
    line += ",\"p\":";
    serialiseList(propList, line);
    line += "}\n";
    std::fwrite(line.data(), 1, line.size(), stdout);
  }

  void emitText(const char *event, const librevenge::RVNGString &text)
  {
    std::string line = "{\"e\":";
    jsonString(event, line);
    line += ",\"t\":";
    jsonString(text.cstr(), line);
    line += "}\n";
    std::fwrite(line.data(), 1, line.size(), stdout);
  }

public:
  void startDocument(const librevenge::RVNGPropertyList &p) override { emit("startDocument", p); }
  void endDocument() override { emit("endDocument"); }
  void setDocumentMetaData(const librevenge::RVNGPropertyList &p) override { emit("setDocumentMetaData", p); }
  void defineEmbeddedFont(const librevenge::RVNGPropertyList &p) override { emit("defineEmbeddedFont", p); }

  void startPage(const librevenge::RVNGPropertyList &p) override { emit("startPage", p); }
  void endPage() override { emit("endPage"); }
  void startMasterPage(const librevenge::RVNGPropertyList &p) override { emit("startMasterPage", p); }
  void endMasterPage() override { emit("endMasterPage"); }

  void setStyle(const librevenge::RVNGPropertyList &p) override { emit("setStyle", p); }
  void startLayer(const librevenge::RVNGPropertyList &p) override { emit("startLayer", p); }
  void endLayer() override { emit("endLayer"); }
  void startEmbeddedGraphics(const librevenge::RVNGPropertyList &p) override { emit("startEmbeddedGraphics", p); }
  void endEmbeddedGraphics() override { emit("endEmbeddedGraphics"); }
  void openGroup(const librevenge::RVNGPropertyList &p) override { emit("openGroup", p); }
  void closeGroup() override { emit("closeGroup"); }

  void drawRectangle(const librevenge::RVNGPropertyList &p) override { emit("drawRectangle", p); }
  void drawEllipse(const librevenge::RVNGPropertyList &p) override { emit("drawEllipse", p); }
  void drawPolygon(const librevenge::RVNGPropertyList &p) override { emit("drawPolygon", p); }
  void drawPolyline(const librevenge::RVNGPropertyList &p) override { emit("drawPolyline", p); }
  void drawPath(const librevenge::RVNGPropertyList &p) override { emit("drawPath", p); }
  void drawGraphicObject(const librevenge::RVNGPropertyList &p) override { emit("drawGraphicObject", p); }
  void drawConnector(const librevenge::RVNGPropertyList &p) override { emit("drawConnector", p); }

  void startTextObject(const librevenge::RVNGPropertyList &p) override { emit("startTextObject", p); }
  void endTextObject() override { emit("endTextObject"); }

  void startTableObject(const librevenge::RVNGPropertyList &p) override { emit("startTableObject", p); }
  void openTableRow(const librevenge::RVNGPropertyList &p) override { emit("openTableRow", p); }
  void closeTableRow() override { emit("closeTableRow"); }
  void openTableCell(const librevenge::RVNGPropertyList &p) override { emit("openTableCell", p); }
  void closeTableCell() override { emit("closeTableCell"); }
  void insertCoveredTableCell(const librevenge::RVNGPropertyList &p) override { emit("insertCoveredTableCell", p); }
  void endTableObject() override { emit("endTableObject"); }

  void insertTab() override { emit("insertTab"); }
  void insertSpace() override { emit("insertSpace"); }
  void insertText(const librevenge::RVNGString &text) override { emitText("insertText", text); }
  void insertLineBreak() override { emit("insertLineBreak"); }
  void insertField(const librevenge::RVNGPropertyList &p) override { emit("insertField", p); }

  void openOrderedListLevel(const librevenge::RVNGPropertyList &p) override { emit("openOrderedListLevel", p); }
  void openUnorderedListLevel(const librevenge::RVNGPropertyList &p) override { emit("openUnorderedListLevel", p); }
  void closeOrderedListLevel() override { emit("closeOrderedListLevel"); }
  void closeUnorderedListLevel() override { emit("closeUnorderedListLevel"); }
  void openListElement(const librevenge::RVNGPropertyList &p) override { emit("openListElement", p); }
  void closeListElement() override { emit("closeListElement"); }

  void defineParagraphStyle(const librevenge::RVNGPropertyList &p) override { emit("defineParagraphStyle", p); }
  void openParagraph(const librevenge::RVNGPropertyList &p) override { emit("openParagraph", p); }
  void closeParagraph() override { emit("closeParagraph"); }
  void defineCharacterStyle(const librevenge::RVNGPropertyList &p) override { emit("defineCharacterStyle", p); }
  void openSpan(const librevenge::RVNGPropertyList &p) override { emit("openSpan", p); }
  void closeSpan() override { emit("closeSpan"); }
  void openLink(const librevenge::RVNGPropertyList &p) override { emit("openLink", p); }
  void closeLink() override { emit("closeLink"); }
};

} // namespace

int main(int argc, char **argv)
{
  if (argc != 2)
  {
    std::fprintf(stderr, "usage: pubdump <file.pub>\n");
    return 2;
  }

  librevenge::RVNGFileStream input(argv[1]);

  if (!libmspub::MSPUBDocument::isSupported(&input))
  {
    std::fprintf(stderr, "pubdump: unsupported or corrupt Publisher file: %s\n", argv[1]);
    return 3;
  }

  JsonPainter painter;
  if (!libmspub::MSPUBDocument::parse(&input, &painter))
  {
    std::fprintf(stderr, "pubdump: parse failed: %s\n", argv[1]);
    return 4;
  }

  std::fflush(stdout);
  return 0;
}
