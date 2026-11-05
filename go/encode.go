package whitebird

import (
	"bytes"
	"cmp"
	"encoding"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
)

// Parameter structs mark their fields with the tags "query", "header" and "form", holding the
// wire name and optionally ",omitempty". Nil pointers, nil interfaces and empty slices are
// never sent; other zero values are skipped only with omitempty. Slices repeat the parameter.
// In multipart bodies, the tag "mime" sets the content type of a part.

// File is a file to upload, with the name and media type the server receives. Methods that
// upload files take any [io.Reader]; a File adds a name and type to it:
//
//	f, err := os.Open("passport.jpg")
//	...
//	params.Document = whitebird.File{Name: "passport.jpg", ContentType: "image/jpeg", Body: f}
//
// Without a name, the name of an [os.File] is used, or else the name of the field. Without a
// media type, the type is guessed from the extension of the name, then from the content.
type File struct {
	Name        string
	ContentType string
	Body        io.Reader
}

// Read reads from the body of f, so that a File can be passed where an [io.Reader] is expected.
func (f File) Read(p []byte) (int, error) {
	if f.Body == nil {
		return 0, io.EOF
	}
	return f.Body.Read(p)
}

// encodeValues collects the fields of the struct v that carry tag.
func encodeValues(v any, tag string) (url.Values, error) {
	values := url.Values{}
	err := eachTagged(v, tag, func(name string, field reflect.Value, _ reflect.StructTag) error {
		strs, err := formatValues(field)
		values[name] = append(values[name], strs...)
		return err
	})
	return values, err
}

// multipartBody encodes the "form" fields of the struct v as multipart/form-data. Readers
// become file parts (see [File]), and slices of readers one part per reader. Structs and maps
// become JSON parts, as do other values whose "mime" tag is a JSON media type, except strings,
// which are taken to be JSON already. With a "text/plain" tag, structs and maps are sent as
// JSON text in a plain field, as the web application does. Other values become text parts.
func multipartBody(v any) encoder {
	return func() (string, []byte, error) {
		var buf bytes.Buffer
		w := multipart.NewWriter(&buf)
		err := eachTagged(v, "form", func(name string, field reflect.Value, tag reflect.StructTag) error {
			return writePart(w, name, tag.Get("mime"), field)
		})
		if closeErr := w.Close(); err == nil {
			err = closeErr
		}
		return w.FormDataContentType(), buf.Bytes(), err
	}
}

func writePart(w *multipart.Writer, name, contentType string, field reflect.Value) error {
	switch v := field.Interface().(type) {
	case io.Reader:
		return writeFile(w, name, contentType, v)
	case []io.Reader:
		for _, r := range v {
			if err := writeFile(w, name, contentType, r); err != nil {
				return err
			}
		}
		return nil
	}
	for field.Kind() == reflect.Pointer || field.Kind() == reflect.Interface {
		field = field.Elem()
	}
	object := isObject(field.Type())
	if contentType == "" && object {
		contentType = "application/json"
	}
	if isPlainText(contentType) {
		// A plain field, which carries no content type of its own.
		contentType = ""
		if object {
			data, err := json.Marshal(field.Interface())
			if err != nil {
				return err
			}
			return writeField(w, name, "", data)
		}
	}
	if isJSON(contentType) {
		data := []byte(field.String())
		if field.Kind() != reflect.String {
			var err error
			if data, err = json.Marshal(field.Interface()); err != nil {
				return err
			}
		}
		return writeField(w, name, contentType, data)
	}
	strs, err := formatValues(field)
	for _, s := range strs {
		if err == nil {
			err = writeField(w, name, contentType, []byte(s))
		}
	}
	return err
}

func writeField(w *multipart.Writer, name, contentType string, data []byte) error {
	header := textproto.MIMEHeader{"Content-Disposition": {`form-data; name="` + quoteEscaper.Replace(name) + `"`}}
	if contentType != "" {
		header.Set("Content-Type", contentType)
	}
	part, err := w.CreatePart(header)
	if err == nil {
		_, err = part.Write(data)
	}
	return err
}

var quoteEscaper = strings.NewReplacer("\\", "\\\\", `"`, "\\\"")

func writeFile(w *multipart.Writer, name, contentType string, r io.Reader) error {
	filename, body := name, r
	var fileType string
	switch f := r.(type) {
	case File:
		filename, fileType, body = cmp.Or(f.Name, filename), f.ContentType, f
	case *File:
		filename, fileType, body = cmp.Or(f.Name, filename), f.ContentType, f
	case interface{ Name() string }:
		filename = filepath.Base(f.Name())
	}
	switch {
	case fileType != "":
		contentType = fileType
	case !isConcrete(contentType):
		contentType = mime.TypeByExtension(filepath.Ext(filename))
	}
	if contentType == "" {
		// Guess from the content, as the server may check it.
		head := make([]byte, 512)
		n, err := io.ReadFull(body, head)
		if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
			return err
		}
		contentType = http.DetectContentType(head[:n])
		body = io.MultiReader(bytes.NewReader(head[:n]), body)
	}
	header := textproto.MIMEHeader{
		"Content-Disposition": {multipart.FileContentDisposition(name, filename)},
		"Content-Type":        {contentType},
	}
	part, err := w.CreatePart(header)
	if err != nil {
		return err
	}
	_, err = io.Copy(part, body)
	return err
}

// isConcrete reports whether contentType names a specific type, rather than a range or the
// generic binary type.
func isConcrete(contentType string) bool {
	mediaType, _, _ := strings.Cut(contentType, ";")
	mediaType = strings.TrimSpace(mediaType)
	return mediaType != "" && !strings.Contains(mediaType, "*") && mediaType != "application/octet-stream"
}

func isPlainText(contentType string) bool {
	mediaType, _, _ := strings.Cut(contentType, ";")
	return strings.TrimSpace(mediaType) == "text/plain"
}

var textMarshaler = reflect.TypeFor[encoding.TextMarshaler]()

// isObject reports whether values of type t encode as JSON objects or arrays of them.
func isObject(t reflect.Type) bool {
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	if t.Implements(textMarshaler) || reflect.PointerTo(t).Implements(textMarshaler) {
		return false
	}
	switch t.Kind() {
	case reflect.Struct, reflect.Map:
		return true
	case reflect.Slice, reflect.Array:
		return isObject(t.Elem())
	}
	return false
}

func isJSON(contentType string) bool {
	mediaType, _, _ := strings.Cut(contentType, ";")
	mediaType = strings.TrimSpace(mediaType)
	return mediaType == "application/json" || strings.HasSuffix(mediaType, "+json")
}

func eachTagged(v any, tag string, fn func(name string, field reflect.Value, tags reflect.StructTag) error) error {
	rv := reflect.ValueOf(v)
	for rv.Kind() == reflect.Pointer {
		if rv.IsNil() {
			return nil
		}
		rv = rv.Elem()
	}
	if rv.Kind() != reflect.Struct {
		return nil
	}
	rt := rv.Type()
	for i := range rt.NumField() {
		tags := rt.Field(i).Tag
		spec, ok := tags.Lookup(tag)
		if !ok {
			continue
		}
		name, option, _ := strings.Cut(spec, ",")
		field := rv.Field(i)
		switch {
		case (field.Kind() == reflect.Pointer || field.Kind() == reflect.Interface) && field.IsNil():
			continue
		case option == "omitempty" && field.IsZero():
			continue
		}
		if err := fn(name, field, tags); err != nil {
			return fmt.Errorf("%s %s: %w", tag, name, err)
		}
	}
	return nil
}

func formatValues(v reflect.Value) ([]string, error) {
	switch v.Kind() {
	case reflect.Pointer, reflect.Interface:
		if v.IsNil() {
			return nil, nil
		}
		return formatValues(v.Elem())
	case reflect.Slice, reflect.Array:
		strs := make([]string, 0, v.Len())
		for i := range v.Len() {
			s, err := formatValue(v.Index(i))
			if err != nil {
				return nil, err
			}
			strs = append(strs, s)
		}
		return strs, nil
	}
	s, err := formatValue(v)
	return []string{s}, err
}

func formatValue(v reflect.Value) (string, error) {
	if m, ok := v.Interface().(encoding.TextMarshaler); ok {
		text, err := m.MarshalText()
		return string(text), err
	}
	switch v.Kind() {
	case reflect.String:
		return v.String(), nil
	case reflect.Bool:
		return strconv.FormatBool(v.Bool()), nil
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return strconv.FormatInt(v.Int(), 10), nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return strconv.FormatUint(v.Uint(), 10), nil
	case reflect.Float32, reflect.Float64:
		return strconv.FormatFloat(v.Float(), 'f', -1, v.Type().Bits()), nil
	}
	return "", fmt.Errorf("unsupported type %s", v.Type())
}
