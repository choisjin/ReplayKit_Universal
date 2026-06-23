import { Image, Typography } from 'antd';
import { Announcement, announcementImages, isGuide, stepImage, stepText } from '../lib/manager';

/**
 * 공지 본문 공용 렌더러 (매니저 공개 페이지의 AnnouncementBody 와 동일한 레이아웃).
 *  - 일반 공지(notice): 본문 + 이미지 갤러리 그리드 (클릭 시 확대, Image.PreviewGroup)
 *  - 단계별 가이드(guide): 개요(content, 선택) + 번호 뱃지 + 글 + 이미지 세로 단계 레이아웃
 *  - 하위호환: images 없으면 image_data(단일) 사용. type 없으면 notice 처리.
 */
export default function AnnouncementBody({ ann }: { ann: Announcement }) {
  if (isGuide(ann)) {
    return (
      <div>
        {ann.content && (
          <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 14 }}>
            {ann.content}
          </Typography.Paragraph>
        )}
        <Image.PreviewGroup>
          {ann.steps!.map((s, i) => {
            const img = stepImage(s);
            const text = stepText(s);
            return (
              <div key={i} style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
                <div
                  style={{
                    flexShrink: 0,
                    width: 24,
                    height: 24,
                    borderRadius: '50%',
                    background: '#1677ff',
                    color: '#fff',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 12,
                    fontWeight: 700,
                  }}
                >
                  {i + 1}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {text && (
                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: img ? 6 : 0 }}>
                      {text}
                    </Typography.Paragraph>
                  )}
                  {img && (
                    <Image src={img} alt={`step ${i + 1}`} style={{ maxWidth: '100%', borderRadius: 6 }} />
                  )}
                </div>
              </div>
            );
          })}
        </Image.PreviewGroup>
      </div>
    );
  }

  // 일반 공지
  const imgs = announcementImages(ann);
  return (
    <div>
      {ann.content && (
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: imgs.length > 0 ? 10 : 0 }}>
          {ann.content}
        </Typography.Paragraph>
      )}
      {imgs.length > 0 && (
        <Image.PreviewGroup>
          {imgs.length === 1 ? (
            <Image src={imgs[0]} style={{ maxWidth: '100%', borderRadius: 6, display: 'block' }} />
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))',
                gap: 8,
              }}
            >
              {imgs.map((src, i) => (
                <Image
                  key={i}
                  src={src}
                  alt={`image ${i + 1}`}
                  style={{ width: '100%', height: 96, objectFit: 'cover', borderRadius: 6 }}
                />
              ))}
            </div>
          )}
        </Image.PreviewGroup>
      )}
    </div>
  );
}
